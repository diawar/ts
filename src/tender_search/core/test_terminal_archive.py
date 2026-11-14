from datetime import timedelta
from importlib import import_module
from unittest.mock import patch

from django.apps import apps
from django.test import TestCase, override_settings
from django.utils import timezone

from .fulfillment import archive_due, advance, complete_payment
from .models import (CollectionLot, ContractWatch, LotShipment, SearchProfile,
                     UserLotWorkflow)
from .workflow_state import update_workflow


@override_settings(STORAGES={
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
})
class TerminalArchiveTests(TestCase):
    def setUp(self):
        from .test_lot_pricing import LotPricingTests
        LotPricingTests.setUp(self)
        self.second = SearchProfile.objects.create(owner=self.owner, name='Ещё одна подборка')
        CollectionLot.objects.create(profile=self.second, lot=self.lot)
        self.state = UserLotWorkflow.objects.get(owner=self.owner, lot=self.lot)
        self.now = timezone.now()

    def enter(self, stage, age):
        state = update_workflow(self.owner.pk, self.lot.pk, stage=stage,
                                outcome='won' if stage == 'won' else '', result_tracking=False)
        UserLotWorkflow.objects.filter(pk=state.pk).update(stage_entered_at=self.now-age)
        return state

    def test_all_terminal_stages_archive_at_exactly_30_days_and_preserve_owner(self):
        for stage in ('won', 'lost', 'not_participated'):
            with self.subTest(stage=stage):
                self.enter(stage, timedelta(days=30))
                with patch('tender_search.core.fulfillment.timezone.now', return_value=self.now):
                    archive_due()
                self.state.refresh_from_db()
                self.assertEqual(self.state.stage, 'archived')
                self.assertEqual(self.state.archived_from_stage, stage)
                self.assertEqual(self.state.archived_at, self.now)
                self.assertEqual(set(CollectionLot.objects.filter(profile__owner=self.owner).values_list('stage', flat=True)), {'archived'})
                self.assertEqual(UserLotWorkflow.objects.get(owner=self.other, lot=self.lot).stage, 'preparing')

    def test_not_before_30_days_and_never_other_stages(self):
        for stage in ('won', 'lost', 'not_participated'):
            self.enter(stage, timedelta(days=30)-timedelta(seconds=1))
            with patch('tender_search.core.fulfillment.timezone.now', return_value=self.now):
                archive_due()
            self.state.refresh_from_db()
            self.assertEqual(self.state.stage, stage)
        for stage in ('new', 'preparing', 'preparing', 'awaiting_results', 'applied', 'contract', 'delivery', 'payment'):
            self.enter(stage, timedelta(days=90))
            archive_due()
            self.state.refresh_from_db()
            self.assertEqual(self.state.stage, stage)

    def test_refreshes_and_new_membership_do_not_restart_clock_but_transition_does(self):
        old = self.now-timedelta(days=29)
        self.enter('lost', timedelta(days=29))
        update_workflow(self.owner.pk, self.lot.pk, stage='lost', outcome='lost')
        item = CollectionLot.objects.filter(profile__owner=self.owner).first()
        item.stage='lost'; item.save(update_fields=['stage', 'outcome', 'updated_at'])
        profile = SearchProfile.objects.create(owner=self.owner, name='Новая подборка')
        CollectionLot.objects.create(profile=profile, lot=self.lot)
        self.state.refresh_from_db()
        self.assertEqual(self.state.stage_entered_at, old)
        update_workflow(self.owner.pk, self.lot.pk, stage='contract')
        self.state.refresh_from_db()
        self.assertGreaterEqual(self.state.stage_entered_at, self.now)
        item.refresh_from_db(); item.stage='delivery'; item.save(update_fields=['stage'])
        self.state.refresh_from_db()
        self.assertEqual(self.state.stage, 'delivery')
        self.assertGreaterEqual(self.state.stage_entered_at, self.now)

    def test_restore_preserves_completion_and_starts_new_30_days(self):
        self.enter('won', timedelta(days=31))
        UserLotWorkflow.objects.filter(pk=self.state.pk).update(completed_at=self.now-timedelta(days=31))
        archive_due()
        advance(self.owner.pk, self.lot.pk, 'restore')
        archive_due()
        self.state.refresh_from_db()
        self.assertEqual(self.state.stage, 'won')
        self.assertEqual(self.state.completed_at, self.now-timedelta(days=31))
        self.assertFalse(self.state.result_tracking)
        self.assertIsNone(self.state.archived_at)
        self.assertGreaterEqual(self.state.stage_entered_at, self.now)

    def test_stage_views_and_counters_are_exclusive_and_deduplicate_memberships(self):
        for stage in ('contract', 'delivery', 'payment', 'won', 'archived'):
            update_workflow(self.owner.pk, self.lot.pk, stage=stage, outcome='won')
            for selected in ('contract', 'won', 'archived'):
                response=self.client.get('/workspace/', {'stage': selected, 'profile': 'all'})
                self.assertEqual(response.context['page'].paginator.count, int(stage == selected))
                counts={step['key']: step['count'] for step in response.context['steps']}
                self.assertEqual(counts['won'], int(stage == 'won'))
                self.assertEqual(counts[stage], 1)
                self.assertEqual(sum(counts.values()), 1)

    def test_full_payment_without_confirmed_shipment_or_dated_act_cannot_complete(self):
        state=update_workflow(self.owner.pk, self.lot.pk, stage='payment', outcome='won')
        ContractWatch.objects.create(workflow=state, state='paid', evidence={'state': 'paid'}, checked_at=self.now)
        shipment=LotShipment.objects.create(owner=self.owner, lot=self.lot, act='act.pdf')
        for shipped, accepted in ((None, None), (self.now, None), (None, self.now)):
            shipment.shipped_at=shipped; shipment.act_uploaded_at=accepted; shipment.save()
            with self.assertRaises(ValueError):
                complete_payment(self.owner.pk, self.lot.pk)
            self.state.refresh_from_db()
            self.assertEqual(self.state.stage, 'payment')
            self.assertIsNone(self.state.completed_at)

    def test_orphan_workflow_is_excluded_from_archive_batch(self):
        self.enter('lost', timedelta(days=31))
        CollectionLot.objects.filter(profile__owner=self.owner).delete()
        with patch('tender_search.core.fulfillment._advance') as move:
            archive_due()
        move.assert_not_called()

    def test_completed_deal_keeps_read_only_shipment_details(self):
        from .shipping import context
        self.enter('won', timedelta(days=1))
        shipment=LotShipment.objects.create(owner=self.owner, lot=self.lot, mode='courier',
            courier='Courier', tracking_number='tracking', shipped_at=self.now,
            act='act.pdf', act_uploaded_at=self.now)
        details=context(self.owner, self.lot)
        self.assertEqual(details['shipment'], shipment)
        self.assertFalse(details['shipment_editable'])
        self.assertEqual(list(details['shared_acts']), [shipment])

    def test_migration_distinguishes_protocol_win_from_completed_paid_history(self):
        migration=import_module('tender_search.core.migrations.0055_terminal_stage_archive')
        update_workflow(self.owner.pk, self.lot.pk, stage='won', outcome='won')
        other=update_workflow(self.other.pk, self.lot.pk, stage='archived', outcome='won',
            archived_from_stage='payment', completed_at=self.now-timedelta(days=10), archived_at=self.now)
        migration.reconcile(apps, None)
        self.state.refresh_from_db(); other.refresh_from_db()
        self.assertEqual(self.state.stage, 'contract')
        self.assertEqual(set(CollectionLot.objects.filter(profile__owner=self.owner).values_list('stage', flat=True)), {'contract'})
        self.assertEqual(other.stage, 'won')
        self.assertEqual(other.stage_entered_at, other.completed_at)
        self.assertFalse(other.result_tracking)
        self.assertIsNone(other.archived_at)
        update_workflow(self.other.pk, self.lot.pk, stage='archived', archived_from_stage='payment',
            completed_at=self.now-timedelta(days=31), archived_at=self.now)
        migration.reconcile(apps, None)
        other.refresh_from_db()
        self.assertEqual(other.stage, 'archived')
        self.assertEqual(other.archived_from_stage, 'won')
