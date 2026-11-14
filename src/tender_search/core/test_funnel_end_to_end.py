"""One personal lot through the full funnel; remote evidence is controlled."""
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from .models import (ApplicationPreparation, CompanySettings, ContractWatch,
                     DraftQueueItem, LotOutcomeWatch, LotShipment, UserLotPrice,
                     UserLotWorkflow, CollectionLot)
from .workflow_state import update_workflow


@override_settings(STORAGES={
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
})
class FullFunnelTests(TestCase):
    def test_preparation_submission_win_delivery_payment_archive_and_owner_isolation(self):
        from .test_lot_pricing import LotPricingTests
        from .preparation import save_portal_report
        from .outcome_service import check_outcome
        from .outcome_source import Outcome
        from .contract_service import check_contract
        from .fulfillment import accept_uploaded_act

        LotPricingTests.setUp(self)
        company_bin = '241040001415'
        CompanySettings.objects.create(owner=self.owner, company_bin=company_bin)
        self.lot.announcement.external_id = 100
        self.lot.announcement.deadline = timezone.now()+timedelta(days=1)
        self.lot.announcement.save()

        def assert_stage(expected):
            state = UserLotWorkflow.objects.get(owner=self.owner, lot=self.lot)
            self.assertEqual(state.stage, expected)
            self.assertFalse(CollectionLot.objects.filter(profile__owner=self.owner, lot=self.lot).exclude(stage=expected).exists())
            self.assertEqual(UserLotWorkflow.objects.get(owner=self.other, lot=self.lot).stage, 'preparing')
            return state

        self.client.post('/workspace/drafts/add/', {'lots': [str(self.lot.pk)]})
        assert_stage('preparing')
        self.assertTrue(DraftQueueItem.objects.filter(owner=self.owner, lot=self.lot).exists())
        self.client.post(self.endpoint, {'confirm_discount': 'on', 'unit_gross': '116'})
        ApplicationPreparation.objects.create(owner=self.owner, lot=self.lot, company_bin=company_bin)
        report = {'schema': 'goszakup-status-v1', 'company_bin': company_bin,
                  'announcement_number': self.lot.announcement.number, 'lot_number': self.lot.number,
                  'state': 'draft', 'number': '12345',
                  'url': 'https://zakup.gov.kz/cabinet/purchase/my-applications/12345',
                  'observed_at': timezone.now().isoformat()}
        save_portal_report(self.owner, self.lot.pk, report)
        assert_stage('preparing')
        report.update(state='submitted', observed_at=(timezone.now()+timedelta(seconds=1)).isoformat())
        save_portal_report(self.owner, self.lot.pk, report)
        assert_stage('applied')
        self.assertIsNotNone(UserLotPrice.objects.get(owner=self.owner, lot=self.lot).locked_at)

        self.lot.announcement.deadline = timezone.now()-timedelta(seconds=1)
        self.lot.announcement.save()
        verdict = LotOutcomeWatch.objects.get(owner=self.owner, lot=self.lot)
        LotOutcomeWatch.objects.filter(pk=verdict.pk).update(lease='full-flow')
        with patch('tender_search.core.outcome_service.supported_source', return_value=True), \
                patch('tender_search.core.outcome_service.source_client'), \
                patch('tender_search.core.outcome_service.OutcomePortal') as portal:
            portal.return_value.outcome.return_value = Outcome('won', winner_bin=company_bin)
            check_outcome(verdict.pk, 'full-flow')
        state = assert_stage('contract')
        self.assertEqual(state.outcome, 'won')
        watch = ContractWatch.objects.create(workflow=state)

        def contract(result):
            ContractWatch.objects.filter(pk=watch.pk).update(lease='contract-flow')
            with patch('tender_search.core.contract_service.supported_source', return_value=True), \
                    patch('tender_search.core.contract_service.source_client'), \
                    patch('tender_search.core.contract_service.contract_evidence', return_value=result):
                check_contract(watch.pk, 'contract-flow')

        contract({'state': 'waiting'})
        assert_stage('contract')
        contract({'state': 'delivery', 'contract_id': 1, 'number': '1'})
        assert_stage('delivery')
        LotShipment.objects.create(owner=self.owner, lot=self.lot, shipped_at=timezone.now(),
                                   act='test-only-act.pdf', act_uploaded_at=timezone.now())
        accept_uploaded_act(self.owner.pk, self.lot.pk)
        assert_stage('payment')
        contract({'state': 'payment', 'contract_id': 1, 'number': '1'})
        assert_stage('payment')
        contract({'state': 'paid', 'contract_id': 1, 'number': '1'})
        state = assert_stage('won')
        self.assertEqual(state.outcome, 'won')
        self.assertIsNotNone(state.completed_at)
        self.assertFalse(state.result_tracking)
        self.assertFalse(LotOutcomeWatch.objects.get(pk=verdict.pk).enabled)
        self.assertTrue(ApplicationPreparation.objects.filter(owner=self.owner, lot=self.lot).exists())
        from .fulfillment import archive_due
        UserLotWorkflow.objects.filter(pk=state.pk).update(stage_entered_at=timezone.now()-timedelta(days=30))
        archive_due()
        state = assert_stage('archived')
        self.assertEqual(state.archived_from_stage,'won')
        self.assertTrue(state.completed_at)
