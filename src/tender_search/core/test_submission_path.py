from django.test import TestCase,override_settings
from django.utils import timezone
from datetime import timedelta
from .models import UserLotPrice,UserLotWorkflow,LotOutcomeWatch,ApplicationPreparation,CompanySettings
from .workflow_state import update_workflow
from .preparation import save_portal_report

@override_settings(STORAGES={'default':{'BACKEND':'django.core.files.storage.FileSystemStorage'},'staticfiles':{'BACKEND':'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class SubmissionPathTests(TestCase):
    def setUp(self):
        from .test_lot_pricing import LotPricingTests
        LotPricingTests.setUp(self)
        CompanySettings.objects.create(owner=self.owner,company_bin='241040001415')
        self.url=f'/lots/{self.lot.pk}/preparation/'

    def test_price_only_in_preparation_and_never_submits(self):
        update_workflow(self.owner.pk,self.lot.pk,stage='new')
        self.assertEqual(self.client.post(self.endpoint,{'confirm_discount':'on','unit_gross':'116'}).status_code,403)
        update_workflow(self.owner.pk,self.lot.pk,stage='preparing')
        for value in ['116','232']:
            self.assertEqual(self.client.post(self.endpoint,{'confirm_discount':'on','unit_gross':value}).status_code,302)
            price=UserLotPrice.objects.get(owner=self.owner,lot=self.lot)
            self.assertIsNone(price.locked_at)
            self.assertEqual(UserLotWorkflow.objects.get(owner=self.owner,lot=self.lot).stage,'preparing')
        self.assertFalse(LotOutcomeWatch.objects.exists())
        self.assertContains(self.client.get(self.url),'Сохранить подготовку')
        self.assertNotContains(self.client.get(f'/lots/{self.lot.pk}/'),'id="lot-price-entry"')

    def test_only_submitted_report_moves_and_freezes_price(self):
        update_workflow(self.owner.pk,self.lot.pk,stage='preparing')
        self.client.post(self.endpoint,{'confirm_discount':'on','unit_gross':'116'})
        ApplicationPreparation.objects.create(owner=self.owner,lot=self.lot,company_bin='241040001415')
        report={'schema':'goszakup-status-v1','company_bin':'241040001415','announcement_number':self.lot.announcement.number,'lot_number':self.lot.number,'state':'draft','number':'12345','url':'https://zakup.gov.kz/cabinet/purchase/my-applications/12345','observed_at':timezone.now().isoformat()}
        save_portal_report(self.owner,self.lot.pk,report)
        self.assertEqual(UserLotWorkflow.objects.get(owner=self.owner,lot=self.lot).stage,'preparing')
        self.assertIsNone(UserLotPrice.objects.get(owner=self.owner,lot=self.lot).locked_at)
        report.update(state='submitted',observed_at=(timezone.now()+timedelta(seconds=1)).isoformat())
        save_portal_report(self.owner,self.lot.pk,report)
        self.assertEqual(UserLotWorkflow.objects.get(owner=self.owner,lot=self.lot).stage,'applied')
        self.assertTrue(UserLotPrice.objects.get(owner=self.owner,lot=self.lot).locked_at)
        self.assertEqual(self.client.post(self.endpoint,{'confirm_discount':'on','unit_gross':'232'}).status_code,403)
        self.assertTrue(LotOutcomeWatch.objects.filter(owner=self.owner,lot=self.lot).exists())


    def test_old_manual_applied_mark_returns_to_preparation(self):
        from importlib import import_module
        from django.apps import apps
        from .models import DraftQueueItem,CollectionLot
        update_workflow(self.owner.pk,self.lot.pk,stage='applied')
        UserLotPrice.objects.create(owner=self.owner,lot=self.lot,unit_gross=116,vat_rate=16,locked_at=timezone.now())
        import_module('tender_search.core.migrations.0039_unify_submission_path').reconcile(apps,None)
        self.assertEqual(UserLotWorkflow.objects.get(owner=self.owner,lot=self.lot).stage,'preparing')
        self.assertEqual(CollectionLot.objects.get(profile__owner=self.owner,lot=self.lot).stage,'preparing')
        self.assertIsNone(UserLotPrice.objects.get(owner=self.owner,lot=self.lot).locked_at)
        self.assertTrue(DraftQueueItem.objects.filter(owner=self.owner,lot=self.lot).exists())


    def test_enqueue_returns_to_original_workspace_category(self):
        self.lot.raw={'system_id':1,'purchase_method_id':12}
        self.lot.save()
        self.lot.announcement.external_id=100;self.lot.announcement.save()
        target='/workspace/?profile=all&stage=preparing&sort=end&per_page=100'
        response=self.client.post('/workspace/drafts/add/',{'lots':[str(self.lot.pk)],'return_to':target})
        self.assertEqual(response.url,target)
        self.assertEqual(UserLotWorkflow.objects.get(owner=self.owner,lot=self.lot).stage,'preparing')
        response=self.client.post('/workspace/drafts/add/',{'lots':[str(self.lot.pk)],'return_to':'https://example.test/'})
        self.assertEqual(response.url,'/workspace/drafts/')
