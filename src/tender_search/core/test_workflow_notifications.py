from datetime import timedelta
from unittest.mock import patch
from django.test import TestCase, override_settings
from django.utils import timezone
from .models import (WorkflowDelivery,TelegramSubscription,TelegramDelivery,StatusDelivery,
    OutcomeDelivery,LotOutcomeWatch,UserLotWorkflow,ApplicationPreparation,ContractWatch,LotShipment,CompanySettings,AnnouncementWatch)
from .workflow_state import update_workflow
from .workflow_notifications import queue_event,deliver_workflow_notifications
from .outcome_service import enroll_participation_checks,check_outcome,deliver_outcomes,schedule_outcomes
from .outcome_source import Outcome
from .telegram_service import deliver_pending,TelegramError,handle_update
from .preparation import save_portal_report


@override_settings(TELEGRAM_BOT_TOKEN='test-only',STORAGES={'default':{'BACKEND':'django.core.files.storage.FileSystemStorage'},'staticfiles':{'BACKEND':'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class WorkflowNotificationTests(TestCase):
    def setUp(self):
        from .test_automatic_workflow import AutomaticWorkflowTests,BIN
        AutomaticWorkflowTests.setUp(self)
        self.bin=BIN
        self.sub=TelegramSubscription.objects.create(owner=self.owner,enabled=True,chat_id=12345)

    def submit(self,state='submitted'):
        update_workflow(self.owner.pk,self.lot.pk,stage='preparing')
        ApplicationPreparation.objects.get_or_create(owner=self.owner,lot=self.lot,company_bin=self.bin)
        report={'schema':'goszakup-status-v1','company_bin':self.bin,'announcement_number':self.lot.announcement.number,
            'lot_number':self.lot.number,'state':state,'number':'12345',
            'url':'https://zakup.gov.kz/cabinet/purchase/my-applications/12345','observed_at':timezone.now().isoformat()}
        save_portal_report(self.owner,self.lot.pk,report)

    def contract_check(self,result):
        from .contract_service import check_contract
        state=UserLotWorkflow.objects.get(owner=self.owner,lot=self.lot)
        watch,_=ContractWatch.objects.get_or_create(workflow=state)
        ContractWatch.objects.filter(pk=watch.pk).update(lease='test')
        with patch('tender_search.core.contract_service.source_client'),patch('tender_search.core.contract_service.contract_evidence',return_value=result):
            check_contract(watch.pk,'test')

    def won(self):
        from .outcome_service import register_watch
        update_workflow(self.owner.pk,self.lot.pk,stage='contract',outcome='won',result_tracking=True)
        watch=register_watch(self.owner.pk,self.lot.pk)
        LotOutcomeWatch.objects.filter(pk=watch.pk).update(state='won',winner_bin=self.bin,checked_at=timezone.now())

    def test_submission_is_confirmed_private_and_idempotent(self):
        self.submit('draft');self.assertFalse(WorkflowDelivery.objects.exists())
        self.submit();self.submit()
        self.assertEqual(WorkflowDelivery.objects.filter(kind='submitted').count(),1)
        with patch('tender_search.core.workflow_notifications.telegram_api',return_value={'message_id':1}) as send:
            deliver_workflow_notifications();deliver_workflow_notifications()
        send.assert_called_once();self.assertEqual(send.call_args.args[1]['chat_id'],self.sub.chat_id)
        self.assertIn('Заявка подана',send.call_args.args[1]['text'])

    def test_draft_and_unknown_never_notify_submission(self):
        self.submit('draft')
        self.submit('unknown')
        self.assertFalse(WorkflowDelivery.objects.exists())

    def test_signed_contract_only_after_verified_transition_and_once(self):
        self.won()
        self.contract_check({'state':'review','message':'Need verification'})
        self.assertFalse(WorkflowDelivery.objects.exists())
        result={'state':'delivery','contract_id':90,'number':'900','status':'Signed'}
        self.contract_check(result);self.contract_check(result)
        self.assertEqual(UserLotWorkflow.objects.get(owner=self.owner,lot=self.lot).stage,'delivery')
        self.assertEqual(WorkflowDelivery.objects.filter(kind='signed').count(),1)
        with patch('tender_search.core.workflow_notifications.telegram_api',return_value={'message_id':2}) as send:
            deliver_workflow_notifications();deliver_workflow_notifications()
        send.assert_called_once();self.assertIn('Договор подписан',send.call_args.args[1]['text'])

    def test_paid_notification_in_won_and_later_archive_does_not_repeat(self):
        self.won()
        result={'state':'paid','contract_id':90,'number':'900','status':'Executed'}
        self.contract_check(result)
        self.assertFalse(WorkflowDelivery.objects.filter(kind='paid').exists())
        LotShipment.objects.create(owner=self.owner,lot=self.lot,mode='own',act='accepted.pdf',shipped_at=timezone.now(),act_uploaded_at=timezone.now())
        self.contract_check(result)
        state=UserLotWorkflow.objects.get(owner=self.owner,lot=self.lot)
        self.assertEqual(state.stage,'won');self.assertTrue(state.completed_at)
        self.assertFalse(LotOutcomeWatch.objects.get(owner=self.owner,lot=self.lot).enabled)
        with patch('tender_search.core.workflow_notifications.telegram_api',return_value={'message_id':3}) as send:
            deliver_workflow_notifications();deliver_workflow_notifications()
            from .fulfillment import archive_due
            UserLotWorkflow.objects.filter(pk=state.pk).update(stage_entered_at=timezone.now()-timedelta(days=31))
            archive_due();deliver_workflow_notifications()
        state.refresh_from_db();self.assertEqual(state.stage,'archived')
        send.assert_called_once();self.assertIn('Оплата получена',send.call_args.args[1]['text'])
        self.assertIn('«Выиграли»',send.call_args.args[1]['text'])

    def test_nonparticipation_final_message_survives_watch_shutdown(self):
        self.lot.announcement.deadline=timezone.now()-timedelta(minutes=1);self.lot.announcement.save()
        enroll_participation_checks()
        watch=LotOutcomeWatch.objects.get(owner=self.owner,lot=self.lot)
        LotOutcomeWatch.objects.filter(pk=watch.pk).update(lease='test')
        with patch('tender_search.core.outcome_service.source_client'),patch('tender_search.core.outcome_service.OutcomePortal') as source:
            source.return_value.outcome.return_value=Outcome('not_listed',participants_complete=True)
            check_outcome(watch.pk,'test')
        schedule_outcomes()
        watch.refresh_from_db();self.assertFalse(watch.enabled)
        with patch('tender_search.core.outcome_service.telegram_api',return_value={'message_id':4}) as send:
            deliver_outcomes();deliver_outcomes()
        send.assert_called_once();self.assertIn('Не участвовали',send.call_args.args[1]['text'])

    def test_unwanted_new_lot_messages_are_cancelled(self):
        self.profile.enabled=True;self.profile.save()
        for reason in ('expired','declined','closed','closed_workflow'):
            update_workflow(self.owner.pk,self.lot.pk,stage='new')
            self.lot.announcement.deadline=timezone.now()+timedelta(days=1);self.lot.announcement.save()
            self.lot.raw=dict(self.lot.raw,status={'id':6});self.lot.save()
            item=TelegramDelivery.objects.create(owner=self.owner,profile=self.profile,lot=self.lot,announcement_key=reason)
            if reason=='expired':
                self.lot.announcement.deadline=timezone.now()-timedelta(seconds=1);self.lot.announcement.save()
            elif reason=='declined':update_workflow(self.owner.pk,self.lot.pk,stage='not_participated')
            elif reason=='closed_workflow':update_workflow(self.owner.pk,self.lot.pk,stage='closed')
            else:self.lot.raw['status']={'id':16};self.lot.save()
            with patch('tender_search.core.telegram_service.in_work_hours',return_value=True),patch('tender_search.core.telegram_service.telegram_api') as send:
                deliver_pending();send.assert_not_called()
            item.refresh_from_db();self.assertEqual(item.state,'cancelled')

    def test_stop_and_disconnect_cancel_every_pending_queue(self):
        for action in ('stop','disconnect'):
            self.sub.enabled=True;self.sub.chat_id=12345;self.sub.save()
            self.submit()
            WorkflowDelivery.objects.all().delete()
            queue_event(self.owner.pk,self.lot,self.bin,'submitted',action)
            watch=LotOutcomeWatch.objects.get(owner=self.owner,lot=self.lot)
            outcome=OutcomeDelivery.objects.create(watch=watch,generation=watch.generation,event_key=action)
            status_watch=AnnouncementWatch.objects.get(owner=self.owner,announcement=self.lot.announcement)
            status=StatusDelivery.objects.create(watch=status_watch,generation=status_watch.generation,event_key=action)
            new=TelegramDelivery.objects.create(owner=self.owner,profile=self.profile,lot=self.lot,announcement_key=action)
            if action=='stop':handle_update({'message':{'chat':{'id':12345,'type':'private'},'text':'/stop'}})
            else:self.client.post('/settings/',{'action':'disconnect'})
            for item in (outcome,status,new,WorkflowDelivery.objects.get()):
                item.refresh_from_db();self.assertEqual(item.state,'cancelled')

    def test_disconnected_event_is_not_delivered_on_reconnection(self):
        self.sub.enabled=False;self.sub.save()
        self.submit()
        self.sub.enabled=True;self.sub.save()
        self.submit()
        with patch('tender_search.core.workflow_notifications.telegram_api') as send:
            deliver_workflow_notifications();send.assert_not_called()
        self.assertEqual(WorkflowDelivery.objects.get().state,'cancelled')

    def test_obsolete_events_and_changed_company_are_not_delivered(self):
        self.submit()
        update_workflow(self.owner.pk,self.lot.pk,stage='contract',outcome='won')
        with patch('tender_search.core.workflow_notifications.telegram_api') as send:
            deliver_workflow_notifications();send.assert_not_called()
        self.assertEqual(WorkflowDelivery.objects.get().state,'cancelled')
        update_workflow(self.owner.pk,self.lot.pk,stage='applied')
        queue_event(self.owner.pk,self.lot,self.bin,'submitted','second')
        CompanySettings.objects.filter(owner=self.owner).update(company_bin='111111111111')
        with patch('tender_search.core.workflow_notifications.telegram_api') as send:
            deliver_workflow_notifications();send.assert_not_called()
        self.assertFalse(WorkflowDelivery.objects.filter(state='pending').exists())

    def test_uncertain_send_does_not_repeat_and_rate_limit_retries_same_event(self):
        self.submit()
        item=WorkflowDelivery.objects.get()
        with patch('tender_search.core.workflow_notifications.telegram_api',side_effect=TelegramError(429,1)) as send:
            deliver_workflow_notifications();deliver_workflow_notifications()
        send.assert_called_once();item.refresh_from_db();self.assertEqual(item.state,'pending')
        WorkflowDelivery.objects.filter(pk=item.pk).update(retry_at=timezone.now()-timedelta(seconds=1))
        with patch('tender_search.core.workflow_notifications.telegram_api',side_effect=TelegramError()) as send:
            deliver_workflow_notifications();deliver_workflow_notifications()
        send.assert_called_once();item.refresh_from_db();self.assertEqual(item.state,'uncertain')
