from datetime import timedelta
from importlib import import_module
from types import SimpleNamespace

from django.apps import apps
from django.db import connection
from django.test import TestCase, override_settings
from django.utils import timezone

from .models import (ApplicationPreparation, CollectionLot, DraftQueueItem,
                     SearchProfile, UserLotNote, UserLotWorkflow)


@override_settings(STORAGES={
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
})
class ParticipationStageTests(TestCase):
    def setUp(self):
        from .test_draft_queue import DraftQueueTests
        DraftQueueTests.setUp(self)

    def test_selected_lot_without_queue_is_visible_once_and_can_be_prepared(self):
        DraftQueueItem.objects.filter(pk=self.item.pk).delete()
        ApplicationPreparation.objects.filter(owner=self.owner, lot=self.lot).delete()
        other_profile = SearchProfile.objects.create(owner=self.owner, name='Вторая подборка')
        CollectionLot.objects.create(profile=other_profile, lot=self.lot)
        response = self.client.get('/workspace/drafts/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['items']), 1)
        self.assertIsNone(response.context['items'][0].pk)
        self.assertFalse(response.context['queue_can_run'])
        self.assertContains(response, 'Отобран к участию', count=1)
        self.assertContains(response, 'Подготовить заявку')
        self.assertNotContains(response, 'data-queue-id=')
        self.assertEqual(next(step['count'] for step in response.context['steps'] if step['key'] == 'preparing'), 1)
        self.assertNotIn('interesting', [step['key'] for step in response.context['steps']])
        self.assertEqual(self.client.post(f'/lots/{self.lot.pk}/preparation/', self.data).status_code, 302)
        self.assertEqual(DraftQueueItem.objects.filter(owner=self.owner, lot=self.lot).count(), 1)
        self.assertEqual(UserLotWorkflow.objects.get(owner=self.owner, lot=self.lot).stage, 'preparing')

    def test_old_stage_link_preserves_filter_sort_and_page_size(self):
        response = self.client.get('/workspace/', {'stage': 'interesting', 'profile': 'all', 'sort': '-amount', 'per_page': 50})
        self.assertEqual(response.status_code, 302)
        from urllib.parse import parse_qs, urlsplit
        target = urlsplit(response['Location'])
        self.assertEqual(target.path, '/workspace/drafts/')
        self.assertEqual(parse_qs(target.query), {'stage': ['preparing'], 'profile': ['all'], 'sort': ['-amount'], 'per_page': ['50']})

    def test_cancel_only_stops_queue_and_keeps_selected_lot_and_saved_data(self):
        generation = self.item.generation
        self.client.post(self.base + 'cancel/')
        self.item.refresh_from_db()
        self.assertEqual(self.item.state, 'cancelled')
        self.assertNotEqual(self.item.generation, generation)
        self.assertEqual(UserLotWorkflow.objects.get(owner=self.owner, lot=self.lot).stage, 'preparing')
        self.assertTrue(ApplicationPreparation.objects.filter(owner=self.owner, lot=self.lot).exists())
        self.assertContains(self.client.get('/workspace/drafts/'), 'Добавить в очередь')

    def test_decline_cancels_queue_but_cannot_interrupt_running_extension(self):
        record = CollectionLot.objects.get(profile__owner=self.owner, lot=self.lot)
        DraftQueueItem.objects.filter(pk=self.item.pk).update(state='running')
        self.client.post(f'/workspace/items/{record.pk}/stage/', {'stage': 'not_participated'})
        self.assertEqual(UserLotWorkflow.objects.get(owner=self.owner, lot=self.lot).stage, 'preparing')
        DraftQueueItem.objects.filter(pk=self.item.pk).update(state='paused')
        self.client.post(f'/workspace/items/{record.pk}/stage/', {'stage': 'not_participated'})
        self.item.refresh_from_db()
        self.assertEqual(self.item.state, 'cancelled')
        self.assertEqual(UserLotWorkflow.objects.get(owner=self.owner, lot=self.lot).stage, 'not_participated')
        self.assertEqual(UserLotWorkflow.objects.get(owner=self.other, lot=self.lot).stage, 'preparing')

    def test_merge_preserves_personal_data_memberships_and_stage_timestamps(self):
        from .migrations import __name__ as migrations_package
        migration = import_module(migrations_package + '.0056_merge_participation_stage')
        entered = timezone.now() - timedelta(days=7)
        UserLotWorkflow.objects.filter(owner=self.owner, lot=self.lot).update(stage='interesting', stage_entered_at=entered)
        CollectionLot.objects.filter(profile__owner=self.owner, lot=self.lot).update(stage='interesting')
        UserLotWorkflow.objects.filter(owner=self.other, lot=self.lot).update(expired_from_stage='interesting', archived_from_stage='interesting')
        note = UserLotNote.objects.create(owner=self.owner, lot=self.lot, text='Сохранить заметку')
        prepared = ApplicationPreparation.objects.get(owner=self.owner, lot=self.lot)
        generation = self.item.generation
        count = CollectionLot.objects.count()
        migration.merge_stages(apps, SimpleNamespace(connection=connection))
        state = UserLotWorkflow.objects.get(owner=self.owner, lot=self.lot)
        self.assertEqual((state.stage, state.stage_entered_at), ('preparing', entered))
        other = UserLotWorkflow.objects.get(owner=self.other, lot=self.lot)
        self.assertEqual((other.expired_from_stage, other.archived_from_stage), ('preparing', 'preparing'))
        self.assertEqual(CollectionLot.objects.count(), count)
        self.assertFalse(CollectionLot.objects.filter(stage='interesting').exists())
        self.item.refresh_from_db(); prepared.refresh_from_db(); note.refresh_from_db()
        self.assertEqual(self.item.generation, generation)
        self.assertEqual(prepared.data['product_name'], self.data['product_name'])
        self.assertEqual(note.text, 'Сохранить заметку')

    def test_closed_unqueued_selection_waits_for_results_without_fake_draft(self):
        from .draft_queue import expire_draft_queue
        DraftQueueItem.objects.filter(pk=self.item.pk).delete()
        ApplicationPreparation.objects.filter(owner=self.owner, lot=self.lot).delete()
        self.lot.announcement.deadline = timezone.now() - timedelta(seconds=1)
        self.lot.announcement.save()
        expire_draft_queue(self.owner)
        self.assertEqual(UserLotWorkflow.objects.get(owner=self.owner, lot=self.lot).stage, 'awaiting_results')
        self.assertFalse(DraftQueueItem.objects.filter(owner=self.owner, lot=self.lot).exists())
        self.assertNotContains(self.client.get('/workspace/drafts/'), self.lot.number)
        response = self.client.get('/workspace/drafts/?stage=awaiting_results')
        self.assertContains(response, self.lot.number)
        self.assertContains(response, 'Проверяем итоги')
