from datetime import timedelta
from uuid import uuid4
from unittest.mock import patch
from django.test import TestCase,override_settings
from django.utils import timezone
from .models import TelegramSubscription,TelegramDelivery,UserLotWorkflow,Favorite,AnnouncementWatch,CollectionLot,TelegramCursor
from .telegram_actions import apply_interest,callback_data
from .telegram_service import handle_update,deliver_pending,poll_updates,TelegramError
from .workflow_state import update_workflow


@override_settings(TELEGRAM_BOT_TOKEN='test-only',STORAGES={'default':{'BACKEND':'django.core.files.storage.FileSystemStorage'},'staticfiles':{'BACKEND':'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class TelegramActionTests(TestCase):
    def setUp(self):
        from .test_automatic_workflow import AutomaticWorkflowTests
        AutomaticWorkflowTests.setUp(self)
        self.sub=TelegramSubscription.objects.create(owner=self.owner,chat_id=12345,enabled=True)
        update_workflow(self.owner.pk,self.lot.pk,stage='new')
        self.lot.announcement.deadline=timezone.now()+timedelta(days=1);self.lot.announcement.save()
        self.delivery=TelegramDelivery.objects.create(owner=self.owner,profile=self.profile,lot=self.lot,
            announcement_key='interest-action',state='sent',message_id=100,interest_token=uuid4())

    def query(self):
        return {'id':'callback-id','from':{'id':12345},'message':{'message_id':100,'chat':{'id':12345,'type':'private'}},'data':callback_data(self.delivery)}

    def stage(self):
        return UserLotWorkflow.objects.get(owner=self.owner,lot=self.lot).stage

    def test_recipient_action_is_idempotent_stars_and_watches_only_own_lot(self):
        query=self.query()
        with patch('tender_search.core.telegram_service.telegram_api',return_value=True) as api:
            handle_update({'callback_query':query});handle_update({'callback_query':query})
        self.assertEqual(self.stage(),'preparing')
        self.assertEqual(Favorite.objects.filter(owner=self.owner,lot=self.lot).count(),1)
        self.assertFalse(Favorite.objects.filter(owner=self.other).exists())
        self.assertTrue(AnnouncementWatch.objects.get(owner=self.owner,announcement=self.lot.announcement).enabled)
        self.assertEqual(CollectionLot.objects.filter(profile__owner=self.owner,lot=self.lot).count(),1)
        self.assertEqual(api.call_count,2)
        self.assertTrue(all(call.args[0]=='answerCallbackQuery' for call in api.call_args_list))
        self.assertIn('уже на этапе «В работе»',api.call_args.args[1]['text'])

    def test_sender_adds_opaque_action_to_actual_new_message(self):
        TelegramDelivery.objects.filter(pk=self.delivery.pk).update(state='pending',interest_token=None,message_id=None)
        self.profile.enabled=True;self.profile.save()
        with patch('tender_search.core.telegram_service.in_work_hours',return_value=True),patch('tender_search.core.telegram_service.telegram_api',return_value={'message_id':100}) as api:
            deliver_pending()
        buttons=api.call_args.args[1]['reply_markup']['inline_keyboard']
        self.assertEqual(buttons[0][0]['text'],'⭐ В работу')
        self.assertLessEqual(len(buttons[0][0]['callback_data'].encode()),64)
        self.delivery.refresh_from_db()
        self.assertEqual(buttons[0][0]['callback_data'],callback_data(self.delivery))
        self.assertTrue(buttons[1][0]['url'])
        apply_interest(self.query());self.assertEqual(self.stage(),'preparing')

    def test_forged_forwarded_and_unconfirmed_messages_cannot_move_lot(self):
        TelegramSubscription.objects.create(owner=self.other,chat_id=999,enabled=True)
        queries=[]
        for key,value in [('data',f'interest:{self.delivery.pk}:{uuid4().hex}'),('data','interest:9999999999999999999:'+uuid4().hex),('data','bad')]:
            q=self.query();q[key]=value;queries.append(q)
        q=self.query();q['from']['id']=999;queries.append(q)
        q=self.query();q['from']['id']=999;q['message']['chat']['id']=999;queries.append(q)
        q=self.query();q['message']['message_id']=101;queries.append(q)
        q=self.query();q['message']['chat']['type']='group';queries.append(q)
        for q in queries:
            apply_interest(q);self.assertEqual(self.stage(),'new')
        for state in ('pending','sending','cancelled','uncertain'):
            TelegramDelivery.objects.filter(pk=self.delivery.pk).update(state=state)
            apply_interest(self.query());self.assertEqual(self.stage(),'new')

    def test_disabled_or_relinked_subscription_does_not_accept_old_button(self):
        self.sub.enabled=False;self.sub.save()
        apply_interest(self.query());self.assertEqual(self.stage(),'new')
        self.sub.enabled=True;self.sub.chat_id=54321;self.sub.save()
        apply_interest(self.query());self.assertEqual(self.stage(),'new')
        self.sub.chat_id=12345;self.sub.save()
        self.owner.is_active=False;self.owner.save()
        apply_interest(self.query());self.assertEqual(self.stage(),'new')

    def test_later_stages_and_archive_never_move_back(self):
        for stage in ('preparing','applied','contract','delivery','payment','won','lost','archived'):
            update_workflow(self.owner.pk,self.lot.pk,stage=stage)
            apply_interest(self.query());self.assertEqual(self.stage(),stage)

    def test_expired_lot_is_not_reopened_but_decline_can_be_reversed(self):
        update_workflow(self.owner.pk,self.lot.pk,stage='not_participated')
        self.lot.announcement.deadline=timezone.now()-timedelta(seconds=1);self.lot.announcement.save()
        apply_interest(self.query());self.assertEqual(self.stage(),'not_participated')
        self.lot.announcement.deadline=timezone.now()+timedelta(days=1);self.lot.announcement.save()
        raw=self.lot.raw
        self.lot.raw=dict(raw,status={'id':16});self.lot.save()
        apply_interest(self.query());self.assertEqual(self.stage(),'not_participated')
        self.lot.raw=raw;self.lot.save()
        apply_interest(self.query());self.assertEqual(self.stage(),'preparing')

    def test_action_can_add_lot_when_not_yet_in_collection(self):
        CollectionLot.objects.filter(profile__owner=self.owner,lot=self.lot).delete()
        UserLotWorkflow.objects.filter(owner=self.owner,lot=self.lot).delete()
        apply_interest(self.query())
        self.assertEqual(self.stage(),'preparing')
        self.assertTrue(CollectionLot.objects.filter(profile__owner=self.owner,lot=self.lot,stage='preparing').exists())

    def test_expired_callback_confirmation_does_not_block_update_cursor(self):
        query=self.query()
        with patch('tender_search.core.telegram_service.telegram_api',side_effect=TelegramError(400)):
            handle_update({'callback_query':query})
        self.assertEqual(self.stage(),'preparing')
        with patch('tender_search.core.telegram_service.telegram_api') as api:
            api.side_effect=[[{'update_id':12,'callback_query':query}],True]
            poll_updates()
        self.assertIn('callback_query',api.call_args_list[0].args[1]['allowed_updates'])
        self.assertEqual(TelegramCursor.objects.get(pk=1).offset,13)
