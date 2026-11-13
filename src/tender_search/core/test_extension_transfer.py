import json
from datetime import timedelta
from django.test import TestCase, Client, override_settings
from django.utils import timezone
from .models import ApplicationPreparation, CompanySettings, UserLotWorkflow
from . import test_preparation


@override_settings(STORAGES={'default':{'BACKEND':'django.core.files.storage.FileSystemStorage'},
    'staticfiles':{'BACKEND':'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class ExtensionTransferTests(TestCase):
    def setUp(self):
        test_preparation.PreparationTests.setUp(self)
        a=self.lot.announcement;a.external_id=12345678;a.raw={'system':{'id':1}};a.save()
        self.lot.raw={'system_id':1};self.lot.save()
        self.endpoint='/settings/extension/report/'
        self.report={'schema':'goszakup-status-v1','company_bin':'241040001415',
            'announcement_number':a.number,'lot_number':self.lot.number,
            'observed_at':timezone.now().isoformat(),'state':'completed','number':'73727651',
            'url':'https://zakup.gov.kz/cabinet/purchase/my-applications/73727651'}

    def post_report(self,report=None):
        return self.client.post(self.endpoint,json.dumps(report or self.report),content_type='application/json')

    def test_get_does_not_write_and_post_requires_csrf(self):
        strict=Client(enforce_csrf_checks=True)
        self.assertEqual(strict.get(self.endpoint).status_code,302)
        strict.force_login(self.owner)
        self.assertEqual(strict.get(self.endpoint).status_code,200)
        self.assertFalse(ApplicationPreparation.objects.exists())
        self.assertEqual(strict.post(self.endpoint,json.dumps(self.report),content_type='application/json').status_code,403)
        token=strict.cookies['csrftoken'].value
        response=strict.post(self.endpoint,json.dumps(self.report),content_type='application/json',HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code,200)
        self.assertEqual(response['Cache-Control'],'no-store, private')
        self.assertEqual(response.json()['state'],'completed')
        self.assertEqual(UserLotWorkflow.objects.get(owner=self.owner,lot=self.lot).stage,'preparing')

    def test_mismatch_or_missing_membership_creates_no_preparation(self):
        bad=dict(self.report,company_bin='123456789012')
        self.assertEqual(self.post_report(bad).status_code,400)
        self.assertFalse(ApplicationPreparation.objects.exists())
        UserLotWorkflow.objects.filter(owner=self.owner,lot=self.lot).delete()
        self.assertEqual(self.post_report().status_code,400)
        self.assertFalse(ApplicationPreparation.objects.exists())

    def test_unknown_preserves_status_and_other_owner_is_separate(self):
        self.assertEqual(self.post_report().status_code,200)
        report=dict(self.report,state='unknown',observed_at=(timezone.now()+timedelta(seconds=1)).isoformat())
        self.assertEqual(self.post_report(report).status_code,200)
        p=ApplicationPreparation.objects.get(owner=self.owner,lot=self.lot)
        self.assertEqual(p.portal_state,'completed');self.assertTrue(p.portal_sync_error)
        self.assertEqual(self.post_report(report).status_code,400)
        self.client.force_login(self.other)
        self.assertEqual(self.post_report().status_code,400)
        self.assertFalse(ApplicationPreparation.objects.filter(owner=self.other).exists())

    def test_bad_link_and_unsupported_source_roll_back_creation(self):
        bad=dict(self.report,url='https://zakup.gov.kz/cabinet/purchase/my-applications/99')
        self.assertEqual(self.post_report(bad).status_code,400)
        self.assertFalse(ApplicationPreparation.objects.exists())
        self.lot.raw={'system_id':3};self.lot.save()
        self.assertEqual(self.post_report().status_code,400)
        self.assertFalse(ApplicationPreparation.objects.exists())

    def test_description_confirmation_is_separate_and_persists_on_reload(self):
        draft=dict(self.report,state='draft',description_saved=True)
        self.assertEqual(self.post_report(draft).status_code,200)
        p=ApplicationPreparation.objects.get(owner=self.owner,lot=self.lot)
        confirmed=p.portal_description_saved_at
        self.assertIsNotNone(confirmed)
        self.assertEqual(UserLotWorkflow.objects.get(owner=self.owner,lot=self.lot).stage,'awaiting_results')
        self.assertContains(self.client.get(f'/lots/{self.lot.pk}/preparation/'),'Черновик заполнен')
        from .lot_state import apply_personal_state
        from .templatetags.search_tags import application_portal_status
        apply_personal_state(self.owner,[self.lot])
        badge=str(application_portal_status(self.lot))
        self.assertIn('Описание товара заполнено',badge);self.assertIn('73727651',badge)
        # A later read of the application list confirms only the draft itself.
        draft.pop('description_saved');draft['observed_at']=(timezone.now()+timedelta(seconds=1)).isoformat()
        self.assertEqual(self.post_report(draft).status_code,200)
        p.refresh_from_db();self.assertEqual(p.portal_description_saved_at,confirmed)
        draft.update(number='73727652',url='https://zakup.gov.kz/cabinet/purchase/my-applications/73727652',observed_at=(timezone.now()+timedelta(seconds=2)).isoformat())
        self.assertEqual(self.post_report(draft).status_code,200)
        p.refresh_from_db();self.assertIsNone(p.portal_description_saved_at)

    def test_submitted_moves_all_memberships_and_enables_results_without_fake_price(self):
        from .models import CollectionLot, LotOutcomeWatch, UserLotPrice, SearchProfile
        extra=SearchProfile.objects.create(owner=self.owner,name='Вторая')
        CollectionLot.objects.create(profile=extra,lot=self.lot)
        other=UserLotWorkflow.objects.get(owner=self.other,lot=self.lot)
        report=dict(self.report,state='submitted')
        response=self.post_report(report)
        self.assertEqual(response.status_code,200);self.assertEqual(response.json()['stage'],'applied')
        workflow=UserLotWorkflow.objects.get(owner=self.owner,lot=self.lot)
        self.assertEqual(workflow.stage,'applied');self.assertTrue(workflow.result_tracking)
        self.assertEqual(set(CollectionLot.objects.filter(profile__owner=self.owner,lot=self.lot).values_list('stage',flat=True)),{'applied'})
        self.assertTrue(LotOutcomeWatch.objects.get(owner=self.owner,lot=self.lot).enabled)
        self.assertFalse(UserLotPrice.objects.filter(owner=self.owner,lot=self.lot).exists())
        other.refresh_from_db();self.assertEqual(other.stage,'preparing')
        self.assertEqual(self.post_report(report).status_code,400)

    def test_submission_freezes_existing_estimate_and_cannot_undo_confirmed_submission(self):
        from .models import UserLotPrice
        price=UserLotPrice.objects.create(owner=self.owner,lot=self.lot,unit_gross='116',vat_rate='16')
        self.assertEqual(self.post_report(dict(self.report,state='submitted')).status_code,200)
        price.refresh_from_db();self.assertIsNotNone(price.locked_at);self.assertEqual(str(price.unit_gross),'116.00')
        self.assertEqual(self.client.post(f'/lots/{self.lot.pk}/application/cancel/').status_code,404)

    def test_report_cannot_move_final_stage_back_or_claim_description_from_unknown(self):
        from .workflow_state import update_workflow
        update_workflow(self.owner.pk,self.lot.pk,stage='contract',outcome='won',result_tracking=True)
        self.assertEqual(self.post_report(dict(self.report,state='submitted')).status_code,200)
        self.assertEqual(UserLotWorkflow.objects.get(owner=self.owner,lot=self.lot).stage,'contract')
        bad=dict(self.report,state='unknown',description_saved=True,observed_at=(timezone.now()+timedelta(seconds=1)).isoformat())
        self.assertEqual(self.post_report(bad).status_code,400)
        bad.update(state='draft',description_saved='true')
        self.assertEqual(self.post_report(bad).status_code,400)
