from unittest.mock import patch
from django.test import TestCase, override_settings
from django.contrib.auth import get_user_model
from .models import CollectionLot, SearchProfile, Favorite, AnnouncementWatch, StatusDelivery
from .search_service import store_lot
from .test_index import row
from .workflow_state import sync_lot, reconcile_workflow_states, add_to_interesting, remove_from_work


@override_settings(STORAGES={'default':{'BACKEND':'django.core.files.storage.FileSystemStorage'},'staticfiles':{'BACKEND':'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class WorkflowStateTests(TestCase):
    def setUp(self):
        self.owner=get_user_model().objects.create_user('stage-owner')
        self.other=get_user_model().objects.create_user('stage-other')
        self.profile=SearchProfile.objects.create(owner=self.owner,name='Работа')
        self.lot=store_lot(row(1))
        self.item=CollectionLot.objects.create(profile=self.profile,lot=self.lot)
        self.client.force_login(self.owner)

    def assert_state(self,favorite,watched):
        state=self.client.get('/lots/ui-state/',{'ids':self.lot.pk}).json()['lots'][0]
        self.assertEqual((state['favorite'],state['watched']),(favorite,watched))
        self.assertEqual(Favorite.objects.filter(owner=self.owner,lot=self.lot).exists(),favorite)

    def test_stage_matrix_and_unchanged_state_never_restarts_notifications(self):
        for stage,favorite,watched in [('new',False,False),('preparing',True,True),('applied',True,True),
            ('contract',True,True),('delivery',True,False),('payment',True,False),
            ('won',True,False),('lost',True,False),('not_participated',False,False)]:
            with self.subTest(stage=stage):
                self.item.stage=stage;self.item.save()
                self.assert_state(favorite,watched)
                watch=AnnouncementWatch.objects.filter(owner=self.owner).first()
                generation=watch.generation if watch else None
                messages=StatusDelivery.objects.count()
                sync_lot(self.owner.pk,self.lot.pk)
                if watch:
                    watch.refresh_from_db();self.assertEqual(watch.generation,generation)
                self.assertEqual(StatusDelivery.objects.count(),messages)

    def test_bulk_changes_reconcile_and_cancel_old_pending_messages(self):
        self.item.stage='preparing';self.item.save()
        watch=AnnouncementWatch.objects.get(owner=self.owner)
        delivery=StatusDelivery.objects.create(watch=watch,generation=watch.generation,event_key='old')
        CollectionLot.objects.filter(pk=self.item.pk).update(stage='not_participated')
        reconcile_workflow_states()
        self.assert_state(False,False)
        delivery.refresh_from_db();self.assertEqual(delivery.state,'cancelled')
        CollectionLot.objects.filter(pk=self.item.pk).update(stage='applied')
        reconcile_workflow_states();self.assert_state(True,True)

    def test_multiple_collections_share_state_but_owners_stay_independent(self):
        self.item.stage='preparing';self.item.save()
        second=SearchProfile.objects.create(owner=self.owner,name='Ещё')
        record=CollectionLot.objects.create(profile=second,lot=self.lot,stage='applied')
        self.assertEqual(record.stage,'preparing')
        foreign=SearchProfile.objects.create(owner=self.other,name='Чужая')
        CollectionLot.objects.create(profile=foreign,lot=self.lot,stage='preparing')
        self.item.stage='not_participated';self.item.save();self.assert_state(False,False)
        record.refresh_from_db();self.assertEqual(record.stage,'not_participated')
        record.stage='won';record.save();self.assert_state(True,False)
        self.assertTrue(AnnouncementWatch.objects.get(owner=self.other).enabled)
        self.profile.delete();second.delete();self.assert_state(False,False)
        self.assertTrue(Favorite.objects.filter(owner=self.other,lot=self.lot).exists())

    def test_protected_star_and_read_only_plane_cannot_override_stage(self):
        self.item.stage='applied';self.item.save()
        self.client.post(f'/lots/{self.lot.pk}/favorite/',{'action':'remove'},HTTP_HX_REQUEST='true')
        self.assert_state(True,True)
        self.client.post(f'/lots/{self.lot.pk}/status-watch/',{'action':'off'},HTTP_HX_REQUEST='true')
        self.assert_state(True,True)
        self.item.stage='preparing';self.item.save()
        self.assertTrue(remove_from_work(self.owner.pk,self.lot.pk))
        self.item.refresh_from_db();self.assertEqual(self.item.stage,'not_participated')
        self.assert_state(False,False)
        add_to_interesting(self.owner.pk,self.lot.pk);self.assert_state(True,True)

    def test_search_star_without_saved_profiles_creates_manual_workspace(self):
        self.profile.delete()
        self.client.post(f'/lots/{self.lot.pk}/favorite/',{'action':'add'})
        item=CollectionLot.objects.get(profile__owner=self.owner,lot=self.lot)
        self.assertEqual(item.stage,'preparing')
        self.assertFalse(item.profile.collection_enabled)
        self.assert_state(True,True)

    def test_inactive_lot_plane_stays_off_when_same_announcement_has_active_lot(self):
        another=store_lot(row(2,announcement_id=self.lot.announcement.source_id))
        CollectionLot.objects.create(profile=self.profile,lot=another,stage='preparing')
        self.assert_state(False,False)
        self.assertTrue(AnnouncementWatch.objects.get(owner=self.owner).enabled)
