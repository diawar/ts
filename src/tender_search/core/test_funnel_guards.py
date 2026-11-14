import json
from datetime import timedelta
from unittest.mock import patch
from django.test import TestCase, Client, override_settings
from django.utils import timezone

STORAGE={'default':{'BACKEND':'django.core.files.storage.FileSystemStorage'},'staticfiles':{'BACKEND':'django.contrib.staticfiles.storage.StaticFilesStorage'}}


@override_settings(TELEGRAM_BOT_TOKEN='test-only',STORAGES=STORAGE)
class StatusRevisionTests(TestCase):
    def setUp(self):
        from .test_status_watch import StatusWatchTests
        StatusWatchTests.setUp(self)
        self.watch=StatusWatchTests.enable(self)

    def scan(self,identifier,name):
        from .test_status_watch import StatusWatchTests
        StatusWatchTests.scan(self,{'id':1000,'status':{'id':identifier,'name':name}})

    def test_delayed_old_status_never_follows_new_status(self):
        from .models import StatusDelivery
        from .status_monitor import deliver_status_notifications
        self.scan(6,'Открыт');self.scan(10,'Завершён')
        old=StatusDelivery.objects.get()
        StatusDelivery.objects.filter(pk=old.pk).update(retry_at=timezone.now()+timedelta(minutes=5))
        self.scan(11,'Итоги опубликованы')
        old.refresh_from_db();self.assertEqual(old.state,'cancelled')
        # Even a legacy/requeued stale event is fenced at delivery time.
        StatusDelivery.objects.filter(pk=old.pk).update(state='pending',retry_at=None)
        with patch('tender_search.core.status_monitor.telegram_api',return_value={'message_id':1}) as send:
            deliver_status_notifications();deliver_status_notifications()
        send.assert_called_once();self.assertIn('Стало: <b>Итоги опубликованы</b>',send.call_args.args[1]['text'])

    def test_inflight_send_finishes_before_the_next_revision(self):
        from .models import StatusDelivery
        from .status_monitor import deliver_status_notifications
        self.scan(6,'Открыт');self.scan(10,'Завершён')
        old=StatusDelivery.objects.get();StatusDelivery.objects.filter(pk=old.pk).update(state='sending')
        self.scan(11,'Итоги')
        with patch('tender_search.core.status_monitor.telegram_api',return_value={'message_id':1}) as send:
            deliver_status_notifications();send.assert_not_called()
            StatusDelivery.objects.filter(pk=old.pk).update(state='sent')
            deliver_status_notifications();send.assert_called_once()

    def test_every_fifteen_minutes_including_previously_daily_watches(self):
        from .models import AnnouncementWatch
        from .status_monitor import schedule_status_checks
        self.scan(6,'Открыт');self.watch.refresh_from_db()
        self.assertEqual(self.watch.next_check_at-self.watch.checked_at,timedelta(minutes=15))
        AnnouncementWatch.objects.filter(pk=self.watch.pk).update(checked_at=timezone.now()-timedelta(minutes=16),next_check_at=timezone.now()+timedelta(days=1))
        with patch('tender_search.core.tasks.check_announcement_status.delay') as enqueue:
            schedule_status_checks();schedule_status_checks()
        enqueue.assert_called_once()

    def test_migration_recovers_revision_and_cancels_legacy_stale_pending(self):
        from importlib import import_module
        from types import SimpleNamespace
        from django.apps import apps
        from django.db import connection
        from .models import StatusDelivery,AnnouncementWatch
        AnnouncementWatch.objects.filter(pk=self.watch.pk).update(revision=2)
        old=StatusDelivery.objects.create(watch=self.watch,generation=self.watch.generation,event_key=f'{self.watch.pk}:{self.watch.generation}:1')
        latest=StatusDelivery.objects.create(watch=self.watch,generation=self.watch.generation,event_key=f'{self.watch.pk}:{self.watch.generation}:2')
        import_module('tender_search.core.migrations.0057_funnel_status_and_launch_guards').reconcile(apps,SimpleNamespace(connection=connection))
        old.refresh_from_db();latest.refresh_from_db()
        self.assertEqual((old.revision,old.state),(1,'cancelled'))
        self.assertEqual((latest.revision,latest.state),(2,'pending'))


@override_settings(TELEGRAM_BOT_TOKEN='test-only',STORAGES=STORAGE)
class ClosedProcurementTests(TestCase):
    def setUp(self):
        from .test_automatic_workflow import AutomaticWorkflowTests
        AutomaticWorkflowTests.setUp(self)

    def test_migration_closes_existing_cancelled_lot_without_losing_personal_records(self):
        from importlib import import_module
        from types import SimpleNamespace
        from django.apps import apps
        from django.db import connection
        from .models import LotOutcomeWatch,UserLotWorkflow,UserLotNote,UserLotPrice,DraftQueueItem
        from .workflow_state import update_workflow
        update_workflow(self.owner.pk,self.lot.pk,stage='awaiting_results',result_tracking=True)
        LotOutcomeWatch.objects.create(owner=self.owner,lot=self.lot,company_bin='241040001415',state='cancelled',checked_at=timezone.now())
        note=UserLotNote.objects.create(owner=self.owner,lot=self.lot,text='Сохранить')
        price=UserLotPrice.objects.create(owner=self.owner,lot=self.lot,unit_gross=100,vat_rate=0)
        queue=DraftQueueItem.objects.create(owner=self.owner,lot=self.lot);generation=queue.generation
        import_module('tender_search.core.migrations.0057_funnel_status_and_launch_guards').reconcile(apps,SimpleNamespace(connection=connection))
        self.assertEqual(UserLotWorkflow.objects.get(owner=self.owner,lot=self.lot).stage,'closed')
        self.assertTrue(UserLotNote.objects.filter(pk=note.pk,text='Сохранить').exists())
        self.assertTrue(UserLotPrice.objects.filter(pk=price.pk,unit_gross=100).exists())
        queue.refresh_from_db();self.assertEqual(queue.state,'cancelled');self.assertNotEqual(queue.generation,generation)

    def test_cancelled_and_unsuccessful_finish_notify_once_and_archive(self):
        from .models import LotOutcomeWatch,UserLotWorkflow,TelegramSubscription
        from .workflow_state import update_workflow
        from .outcome_service import enroll_participation_checks,check_outcome,deliver_outcomes
        from .outcome_source import Outcome
        from .fulfillment import archive_due,advance
        TelegramSubscription.objects.create(owner=self.owner,enabled=True,chat_id=12345)
        self.lot.announcement.deadline=timezone.now()-timedelta(days=1);self.lot.announcement.save()
        for result in ['cancelled','unsuccessful']:
            update_workflow(self.owner.pk,self.lot.pk,stage='preparing',result_tracking=False)
            enroll_participation_checks()
            watch=LotOutcomeWatch.objects.get(owner=self.owner,lot=self.lot)
            LotOutcomeWatch.objects.filter(pk=watch.pk).update(lease='test')
            with patch('tender_search.core.outcome_service.source_client'),patch('tender_search.core.outcome_service.OutcomePortal') as portal:
                portal.return_value.outcome.return_value=Outcome(result)
                check_outcome(watch.pk,'test')
            state=UserLotWorkflow.objects.get(owner=self.owner,lot=self.lot)
            self.assertEqual(state.stage,'closed');self.assertFalse(state.result_tracking)
            watch.refresh_from_db();self.assertFalse(watch.enabled)
            with patch('tender_search.core.outcome_service.telegram_api',return_value={'message_id':1}) as send:
                deliver_outcomes();deliver_outcomes()
            send.assert_called_once();self.assertIn('Закупка закрыта',send.call_args.args[1]['text'])
            self.assertContains(self.client.get('/workspace/?stage=closed'),watch.get_state_display())
            UserLotWorkflow.objects.filter(pk=state.pk).update(stage_entered_at=timezone.now()-timedelta(days=31))
            archive_due();state.refresh_from_db();self.assertEqual(state.stage,'archived')
            advance(self.owner.pk,self.lot.pk,'restore');state.refresh_from_db();self.assertEqual(state.stage,'closed')


@override_settings(STORAGES=STORAGE)
class LaunchReservationTests(TestCase):
    def setUp(self):
        from .test_draft_queue import DraftQueueTests
        DraftQueueTests.setUp(self)

    def issue(self):
        response=self.client.post(self.base+'packet/',{'launch_guard':'1','generation':str(self.item.generation)})
        self.assertEqual(response.status_code,200)
        self.item.refresh_from_db()
        return response.json()['queue_context']['generation']

    def guard(self,generation,**values):
        return self.client.post(self.base+'guard/',json.dumps({'generation':generation,**values}),content_type='application/json')

    def test_launch_reservation_blocks_decline_cancel_edit_and_double_launch(self):
        from .models import CollectionLot,UserLotWorkflow
        generation=self.issue();self.assertEqual(self.item.state,'launching')
        record=CollectionLot.objects.get(profile__owner=self.owner,lot=self.lot)
        self.client.post(f'/workspace/items/{record.pk}/stage/',{'stage':'not_participated'})
        self.client.post(self.base+'cancel/')
        self.item.refresh_from_db();self.assertEqual(self.item.state,'launching')
        self.assertEqual(UserLotWorkflow.objects.get(owner=self.owner,lot=self.lot).stage,'preparing')
        self.assertEqual(self.client.post(self.base+'packet/',{'launch_guard':'1','generation':generation}).status_code,400)
        self.assertEqual(self.client.post(f'/lots/{self.lot.pk}/preparation/',self.data).status_code,403)

    def test_stopped_transfer_invalidates_old_extension_packet(self):
        generation=self.issue()
        response=self.guard(generation,release=True,idle=True);self.assertEqual(response.status_code,200)
        self.assertNotEqual(response.json()['generation'],generation)
        self.assertEqual(self.guard(generation,active=True).status_code,409)
        self.item.refresh_from_db();self.issue()

    def test_action_is_claimed_before_external_click_and_stale_ready_cannot_unlock_it(self):
        generation=self.issue()
        self.assertEqual(self.guard(generation,active=True).status_code,200)
        self.item.refresh_from_db();self.assertEqual(self.item.state,'running')
        self.assertEqual(self.guard(generation,release=True).status_code,409)
        from .test_draft_queue import DraftQueueTests
        self.assertEqual(DraftQueueTests.post_progress(self,status='ready',number='',lotConfirmed=False,description_saved=False).status_code,400)

    def test_guard_requires_owner_csrf_and_current_company(self):
        from .models import CompanySettings
        generation=self.issue()
        strict=Client(enforce_csrf_checks=True);strict.force_login(self.owner)
        self.assertEqual(strict.post(self.base+'guard/',json.dumps({'generation':generation}),content_type='application/json').status_code,403)
        self.assertEqual(self.client.get(self.base+'guard/').status_code,405)
        self.client.force_login(self.other);self.assertEqual(self.guard(generation).status_code,404)
        self.client.force_login(self.owner);CompanySettings.objects.filter(owner=self.owner).update(company_bin='111111111111')
        self.assertEqual(self.guard(generation,active=True).status_code,409)
