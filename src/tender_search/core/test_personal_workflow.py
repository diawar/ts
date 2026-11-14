from datetime import timedelta
from django.test import TestCase, override_settings
from django.utils import timezone
from .models import CollectionLot, SearchProfile, UserLotPrice, UserLotWorkflow, Favorite, AnnouncementWatch
from .workflow_state import add_to_interesting, inherit_workflows, update_workflow
from .outcome_service import move_closed_unsubmitted


@override_settings(STORAGES={'default':{'BACKEND':'django.core.files.storage.FileSystemStorage'},
    'staticfiles':{'BACKEND':'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class PersonalWorkflowTests(TestCase):
    def setUp(self):
        from .test_lot_pricing import LotPricingTests
        LotPricingTests.setUp(self)
        self.item = CollectionLot.objects.get(profile__owner=self.owner,lot=self.lot)

    def test_delete_filter_preserves_applied_price_without_manual_cancel(self):
        # Legacy manual mark remains cancellable; a portal-confirmed bid is covered separately.
        update_workflow(self.owner.pk,self.lot.pk,stage='applied',result_tracking=True)
        UserLotPrice.objects.create(owner=self.owner,lot=self.lot,unit_gross=116,vat_rate=16,locked_at=timezone.now())
        self.client.post(f'/profiles/{self.item.profile_id}/delete/')
        retained=CollectionLot.objects.get(profile__owner=self.owner,lot=self.lot)
        self.assertTrue(retained.profile.filters['manual'])
        self.assertEqual(retained.stage,'applied')
        self.assertTrue(UserLotPrice.objects.get(owner=self.owner,lot=self.lot).locked_at)
        self.assertTrue(AnnouncementWatch.objects.get(owner=self.owner,announcement=self.lot.announcement).enabled)
        self.assertEqual(self.client.post(f'/lots/{self.lot.pk}/application/cancel/').status_code,404)
        retained.refresh_from_db();self.assertEqual(retained.stage,'applied')
        self.assertIsNotNone(UserLotPrice.objects.get(owner=self.owner,lot=self.lot).locked_at)

    def test_switching_watched_lot_invalidates_pending_messages_and_running_check(self):
        from .models import StatusDelivery
        from .search_service import store_lot
        from .workflow_state import update_workflow
        from .status_monitor import check_status
        from unittest.mock import patch
        data=dict(self.lot.raw,id=self.lot.source_id+1,lot_number='second-lot')
        second=store_lot(data)
        self.assertEqual(second.announcement_id,self.lot.announcement_id)
        CollectionLot.objects.create(profile=self.item.profile,lot=second,stage='preparing')
        watch=AnnouncementWatch.objects.get(owner=self.owner,announcement=self.lot.announcement)
        watch.lease='old-check';watch.save()
        generation=watch.generation
        pending=StatusDelivery.objects.create(watch=watch,event_key='old-lot-event',generation=generation,payload={'text':'old'})
        update_workflow(self.owner.pk,self.lot.pk,stage='not_participated')
        watch.refresh_from_db();pending.refresh_from_db()
        self.assertTrue(watch.enabled)
        self.assertEqual(watch.lot_id,second.pk)
        self.assertNotEqual(watch.generation,generation)
        self.assertEqual(pending.state,'cancelled')
        with patch('tender_search.core.status_monitor.ZakupClient') as source:
            check_status(watch.pk,'old-check');source.assert_not_called()

    def test_manual_return_after_deadline_survives_monitor_and_can_be_declined(self):
        self.lot.announcement.deadline=timezone.now()-timedelta(days=1);self.lot.announcement.save()
        move_closed_unsubmitted()
        update_workflow(self.owner.pk,self.lot.pk,stage='not_participated',result_tracking=False)
        self.client.post(f'/workspace/items/{self.item.pk}/stage/',{'stage':'preparing'})
        move_closed_unsubmitted();move_closed_unsubmitted()
        self.item.refresh_from_db();self.assertEqual(self.item.stage,'preparing')
        self.assertTrue(Favorite.objects.filter(owner=self.owner,lot=self.lot).exists())
        self.client.post(f'/workspace/items/{self.item.pk}/stage/',{'stage':'not_participated'})
        self.lot.announcement.deadline=timezone.now()+timedelta(days=1);self.lot.announcement.save()
        move_closed_unsubmitted()
        self.item.refresh_from_db();self.assertEqual(self.item.stage,'not_participated')

    def test_new_membership_inherits_and_action_changes_all_own_memberships(self):
        second=SearchProfile.objects.create(owner=self.owner,name='Second')
        member=CollectionLot.objects.create(profile=second,lot=self.lot)
        self.assertEqual(member.stage,'preparing')
        self.client.post(f'/workspace/items/{member.pk}/stage/',{'stage':'not_participated','return_scope':'all'})
        self.assertEqual(set(CollectionLot.objects.filter(profile__owner=self.owner,lot=self.lot).values_list('stage',flat=True)),{'not_participated'})
        self.assertEqual(CollectionLot.objects.get(profile__owner=self.other,lot=self.lot).stage,'preparing')
        self.assertFalse(Favorite.objects.filter(owner=self.owner,lot=self.lot).exists())
        self.assertFalse(AnnouncementWatch.objects.get(owner=self.owner,announcement=self.lot.announcement).enabled)

    def test_bulk_filter_refresh_inherits_decision_and_does_not_resurrect_declined(self):
        self.client.post(f'/workspace/items/{self.item.pk}/stage/',{'stage':'not_participated'})
        second=SearchProfile.objects.create(owner=self.owner,name='Second')
        CollectionLot.objects.bulk_create(inherit_workflows(self.owner.pk,[CollectionLot(profile=second,lot=self.lot)]))
        self.assertEqual(CollectionLot.objects.get(profile=second).stage,'not_participated')

    def test_aggregate_has_only_one_pair_of_actions(self):
        second=SearchProfile.objects.create(owner=self.owner,name='Second')
        CollectionLot.objects.create(profile=second,lot=self.lot)
        response=self.client.get('/workspace/',{'stage':'preparing'},follow=True)
        self.assertEqual(len(response.context['items']),1)
        self.assertContains(response,'>Не участвовать</button>',count=1)

    def test_future_favorite_still_expires_normally(self):
        self.lot.announcement.deadline=timezone.now()+timedelta(days=1);self.lot.announcement.save()
        add_to_interesting(self.owner.pk,self.lot.pk)
        self.assertFalse(UserLotWorkflow.objects.get(owner=self.owner,lot=self.lot).keep_after_deadline)
        self.lot.announcement.deadline=timezone.now()-timedelta(seconds=1);self.lot.announcement.save()
        move_closed_unsubmitted()
        self.item.refresh_from_db();self.assertEqual(self.item.stage,'awaiting_results');self.assertTrue(self.item.result_tracking)
