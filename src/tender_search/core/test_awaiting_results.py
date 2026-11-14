from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from . import test_draft_queue as fixtures
from .models import ApplicationPreparation, CollectionLot, LotOutcomeWatch, UserLotWorkflow, SearchProfile, Favorite
from .outcome_service import enroll_participation_checks, check_outcome
from .outcome_source import Outcome
from .preparation import save_portal_report


@override_settings(STORAGES={'default':{'BACKEND':'django.core.files.storage.FileSystemStorage'},
                            'staticfiles':{'BACKEND':'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class AwaitingResultsTests(TestCase):
    setUp=fixtures.DraftQueueTests.setUp
    post_progress=fixtures.DraftQueueTests.post_progress

    def workflow(self):
        return UserLotWorkflow.objects.get(owner=self.owner,lot=self.lot)

    def test_only_ready_confirmed_draft_moves_and_all_memberships_follow(self):
        profile=SearchProfile.objects.create(owner=self.owner,name='Ещё одна')
        CollectionLot.objects.create(profile=profile,lot=self.lot)
        other_before=UserLotWorkflow.objects.get(owner=self.other,lot=self.lot).stage
        self.assertEqual(self.post_progress(status='running',description_saved=False).status_code,200)
        self.assertEqual(self.workflow().stage,'preparing')
        response=self.post_progress()
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()['workflow_stage'],'awaiting_results')
        self.assertEqual(self.workflow().stage,'awaiting_results')
        self.assertFalse(self.workflow().result_tracking)
        self.assertFalse(CollectionLot.objects.filter(profile__owner=self.owner,lot=self.lot).exclude(stage='awaiting_results').exists())
        self.assertEqual(UserLotWorkflow.objects.get(owner=self.other,lot=self.lot).stage,other_before)
        self.assertTrue(Favorite.objects.filter(owner=self.owner,lot=self.lot).exists())
        prep=self.client.get('/workspace/drafts/')
        self.assertFalse(prep.context['items'])
        page=self.client.get('/workspace/drafts/?stage=awaiting_results')
        self.assertEqual(len(page.context['items']),1)
        self.assertEqual(next(s['count'] for s in page.context['steps'] if s['key']=='awaiting_results'),1)
        self.assertContains(page,'Ожидаем итоги')
        self.assertNotContains(page,'Проверить черновики на Госзакупе')
        self.assertContains(page,'Наличие сохранённых черновиков проверяется фоном')
        self.assertContains(page,'data-check-ready="true"')
        self.assertContains(self.client.get(f'/lots/{self.lot.pk}/preparation/'),'Просмотр сохранённых данных')
        self.assertEqual(self.client.post(f'/lots/{self.lot.pk}/preparation/',self.data).status_code,403)

    def enroll_closed(self):
        self.post_progress()
        enroll_participation_checks()
        self.assertFalse(self.workflow().result_tracking)
        self.lot.announcement.deadline=timezone.now()-timedelta(seconds=1)
        self.lot.announcement.save()
        enroll_participation_checks()
        self.assertEqual(self.workflow().stage,'awaiting_results')
        self.assertTrue(self.workflow().result_tracking)
        watch=LotOutcomeWatch.objects.get(owner=self.owner,lot=self.lot)
        LotOutcomeWatch.objects.filter(pk=watch.pk).update(lease='awaiting')
        return watch

    def verdict(self,result):
        watch=self.enroll_closed()
        with patch('tender_search.core.outcome_service.source_client'), \
                patch('tender_search.core.outcome_service.supported_source',return_value=True), \
                patch('tender_search.core.outcome_service.OutcomePortal') as portal:
            portal.return_value.outcome.return_value=result
            check_outcome(watch.pk,'awaiting')
        return self.workflow()

    def test_win_moves_to_contract(self):
        self.assertEqual(self.verdict(Outcome('won',winner_bin='241040001415')).stage,'contract')

    def test_loss_moves_only_after_participation_is_confirmed(self):
        self.assertEqual(self.verdict(Outcome('lost',winner_bin='123456789012')).stage,'lost')

    def test_full_nonparticipation_leaves_waiting(self):
        self.assertEqual(self.verdict(Outcome('not_listed',participants_complete=True)).stage,'not_participated')

    def test_incomplete_protocol_never_claims_nonparticipation(self):
        self.assertEqual(self.verdict(Outcome('not_listed',participants_complete=False)).stage,'awaiting_results')

    def test_extension_of_acceptance_keeps_ready_draft_but_pauses_results(self):
        watch=self.enroll_closed()
        self.lot.announcement.deadline=timezone.now()+timedelta(days=1)
        self.lot.announcement.save()
        enroll_participation_checks()
        self.assertEqual(self.workflow().stage,'awaiting_results')
        self.assertFalse(self.workflow().result_tracking)
        watch.refresh_from_db();self.assertFalse(watch.enabled)

    def test_submitted_report_moves_forward_without_duplicate_draft(self):
        self.post_progress()
        saved=ApplicationPreparation.objects.get(owner=self.owner,lot=self.lot)
        save_portal_report(self.owner,self.lot.pk,{'schema':'goszakup-status-v1','state':'submitted',
            'company_bin':saved.company_bin,'lot_number':self.lot.number,'announcement_number':self.lot.announcement.number,
            'number':saved.portal_number,'url':saved.portal_url,'observed_at':(timezone.now()+timedelta(seconds=1)).isoformat()})
        self.assertEqual(self.workflow().stage,'applied')
        self.assertEqual(self.client.post('/workspace/drafts/add/',{'lots':[self.lot.pk]}).status_code,302)
        self.assertEqual(self.workflow().stage,'applied')

    def test_migration_moves_only_confirmed_ready_drafts(self):
        from importlib import import_module
        from django.apps import apps
        from .workflow_state import update_workflow
        self.post_progress()
        update_workflow(self.owner.pk,self.lot.pk,stage='preparing')
        other_before=UserLotWorkflow.objects.get(owner=self.other,lot=self.lot).stage
        import_module('tender_search.core.migrations.0054_awaiting_results_stage').reconcile(apps,None)
        self.assertEqual(self.workflow().stage,'awaiting_results')
        self.assertEqual(UserLotWorkflow.objects.get(owner=self.other,lot=self.lot).stage,other_before)
        self.assertEqual(self.client.post('/workspace/drafts/add/',{'lots':[self.lot.pk]}).status_code,302)
        self.assertEqual(self.workflow().stage,'awaiting_results')
