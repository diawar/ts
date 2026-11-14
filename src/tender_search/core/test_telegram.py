from datetime import datetime,timedelta,timezone as dt_timezone
from hashlib import sha256
from unittest.mock import patch,Mock
from django.contrib.auth import get_user_model
from django.test import TestCase,override_settings
from django.utils import timezone
from tender_search.client import Page
from .models import SearchProfile,TelegramSubscription,TelegramSeen,TelegramDelivery,WebSearch
from .forms import SearchForm
from .search_service import store_lot
from .telegram_service import in_work_hours,connection_link,handle_update,scan_profile,deliver_pending,schedule_profiles,message_for,TelegramError

@override_settings(USE_LOCAL_INDEX=False,TELEGRAM_BOT_TOKEN='test-only-token',TELEGRAM_BOT_USERNAME='test_bot',STORAGES={'default':{'BACKEND':'django.core.files.storage.FileSystemStorage'},'staticfiles':{'BACKEND':'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class TelegramTests(TestCase):
    def setUp(self):
        self.owner=get_user_model().objects.create_user('tg-owner')
        self.other=get_user_model().objects.create_user('tg-other')
        self.values={'terms':'ноут','exclude':'ремонт','mode':'any','kind':'contains','number_kind':'lot_number','depth':'100'}
        form=SearchForm(self.values)
        assert form.is_valid(),form.errors
        self.profile=SearchProfile.objects.create(owner=self.owner,name='Ноутбуки',enabled=True,filters={'form':self.values,'specification':form.specification()},monitor_lease='lease')
        self.sub=TelegramSubscription.objects.create(owner=self.owner,chat_id=1234,enabled=True)

    def row(self, identifier):
        return {'id':identifier,'announcement_id':identifier+1000,'announcement_number':str(identifier+1000),'name_ru':'Ноутбук <script>','lot_number':str(identifier),'total_price':1000,'status_name':'Опубликован','customers':[]}

    def test_almaty_window_edges(self):
        for hour,minute,expected in [(1,59,False),(2,0,True),(13,59,True),(14,0,False)]:
            self.assertEqual(in_work_hours(datetime(2026,10,28,hour,minute,tzinfo=dt_timezone.utc)),expected)

    def test_new_lot_notification_includes_cached_quantity_and_unit_price(self):
        from .models import AnnouncementLotCache
        from decimal import Decimal
        lot=store_lot(self.row(2));lot.amount=Decimal('22000');lot.save()
        AnnouncementLotCache.objects.create(pk=lot.announcement.source_id,rows=[{
            'id':lot.source_id,'is_combined':True,'system':{'id':1},'plan_items':[
                {'id':1,'external_id':100,'count':1,'unit_price':5500,'measure':{'id':1,'name':'Штука'}},
                {'id':2,'external_id':101,'count':3,'unit_price':5500,'measure':{'id':1,'name':'Штука'}},
                {'id':3,'external_id':100,'count':1,'unit_price':5500,'measure':{'id':1,'name':'Штука'}},
            ]}])
        delivery=TelegramDelivery.objects.create(owner=self.owner,profile=self.profile,lot=lot,announcement_key='quantities')
        text=message_for(delivery)['text']
        self.assertIn('Количество: <b>4 шт.</b>',text)
        self.assertIn('Цена за единицу: <b>5 500,00 ₸ без НДС</b>',text)
        self.assertNotIn('<script>',text)

    def test_unknown_quantity_is_not_guessed_from_total(self):
        lot=store_lot(self.row(2))
        delivery=TelegramDelivery.objects.create(owner=self.owner,profile=self.profile,lot=lot,announcement_key='unknown-quantity')
        text=message_for(delivery)['text']
        self.assertIn('Количество: <b>Не уточнено</b>',text)
        self.assertIn('Цена за единицу: <b>Не уточнена</b>',text)

    def test_one_time_private_link(self):
        link=connection_link(self.other)
        token=link.split('start=')[1]
        self.assertEqual(len(token),32)
        self.assertNotEqual(str(self.other.pk),token)
        handle_update({'message':{'chat':{'id':5678,'type':'group'},'text':'/start '+token}})
        self.assertFalse(TelegramSubscription.objects.get(owner=self.other).enabled)
        handle_update({'message':{'chat':{'id':5678,'type':'private'},'text':'/start '+token}})
        sub=TelegramSubscription.objects.get(owner=self.other)
        self.assertEqual(sub.chat_id,5678)
        self.assertTrue(sub.enabled)
        handle_update({'message':{'chat':{'id':9999,'type':'private'},'text':'/start '+token}})
        sub.refresh_from_db();self.assertEqual(sub.chat_id,5678)
        handle_update({'message':{'chat':{'id':5678,'type':'private'},'text':'/stop'}})
        sub.refresh_from_db();self.assertFalse(sub.enabled)

    def test_expired_link_not_bound(self):
        token=connection_link(self.other).split('start=')[1]
        TelegramSubscription.objects.filter(owner=self.other).update(link_expires_at=timezone.now()-timedelta(seconds=1))
        handle_update({'message':{'chat':{'id':5678,'type':'private'},'text':'/start '+token}})
        self.assertIsNone(TelegramSubscription.objects.get(owner=self.other).chat_id)

    @patch('tender_search.core.telegram_service.in_work_hours',return_value=True)
    @patch('tender_search.core.telegram_service.ZakupClient')
    def test_baseline_new_announcements_and_dedup(self,client,working):
        api=client.return_value.__enter__.return_value
        api.announcement_lots.side_effect=lambda aid:[self.row(aid-1000)]
        api.pages.side_effect=lambda **kwargs:iter([Page(1,[self.row(1)],None)])
        scan_profile(self.profile.pk,'lease')
        self.assertEqual(TelegramSeen.objects.count(),1)
        self.assertEqual(TelegramDelivery.objects.count(),0)
        SearchProfile.objects.filter(pk=self.profile.pk).update(monitor_lease='next')
        api.pages.side_effect=lambda **kwargs:iter([Page(2,[self.row(1),self.row(2)],None)])
        scan_profile(self.profile.pk,'next')
        self.assertEqual(TelegramDelivery.objects.count(),1)
        SearchProfile.objects.filter(pk=self.profile.pk).update(monitor_lease='again')
        scan_profile(self.profile.pk,'again')
        self.assertEqual(TelegramDelivery.objects.count(),1)

    @patch('tender_search.core.telegram_service.in_work_hours',return_value=False)
    @patch('tender_search.core.telegram_service.telegram_api')
    @patch('tender_search.core.telegram_service.ZakupClient')
    def test_no_search_or_delivery_at_night(self,client,api,working):
        scan_profile(self.profile.pk,'lease');deliver_pending();schedule_profiles()
        client.assert_not_called();api.assert_not_called()
        self.assertEqual(WebSearch.objects.count(),0)

    @patch('tender_search.core.telegram_service.in_work_hours',return_value=True)
    @patch('tender_search.core.telegram_service.telegram_api',return_value={'message_id':77})
    def test_delivery_escaped_link_and_sent_once(self,api,working):
        lot=store_lot(self.row(2))
        delivery=TelegramDelivery.objects.create(owner=self.owner,profile=self.profile,lot=lot,announcement_key='abc')
        payload=message_for(delivery)
        self.assertTrue(payload['text'].startswith('📂 <b>Подборка: Ноутбуки</b>'))
        self.assertNotIn('<script>',payload['text'])
        self.assertIn('&lt;script&gt;',payload['text'])
        self.assertEqual(payload['reply_markup']['inline_keyboard'][0][0]['url'],'https://zakup.gov.kz/announcement/1002#lot-2')
        deliver_pending();deliver_pending()
        api.assert_called_once()
        delivery.refresh_from_db();self.assertEqual(delivery.state,'sent')

    @patch('tender_search.core.telegram_service.in_work_hours',return_value=True)
    @patch('tender_search.core.telegram_service.telegram_api',side_effect=TelegramError())
    def test_uncertain_delivery_is_not_retried(self,api,working):
        delivery=TelegramDelivery.objects.create(owner=self.owner,profile=self.profile,lot=store_lot(self.row(2)),announcement_key='abc')
        deliver_pending();deliver_pending()
        api.assert_called_once();delivery.refresh_from_db();self.assertEqual(delivery.state,'uncertain')

    def test_owner_isolation_and_hour_units(self):
        self.client.force_login(self.other)
        url=f'/profiles/{self.profile.pk}/notifications/'
        self.assertEqual(self.client.post(url,{'hours':2,'action':'toggle'}).status_code,404)
        self.client.force_login(self.owner)
        self.client.post(url,{'hours':3,'action':'interval'})
        self.profile.refresh_from_db();self.assertEqual(self.profile.interval_minutes,180)
        self.assertEqual(self.client.get(url).status_code,405)
        self.assertContains(self.client.get('/settings/'),'Telegram')
        self.assertContains(self.client.get('/search/'),'profile-notifications')

    @patch('tender_search.core.telegram_service.RedisGate')
    @patch('tender_search.core.telegram_service.in_work_hours',side_effect=[True,False])
    def test_no_api_request_after_wait_crosses_19(self,working,gate):
        from .telegram_service import WorkHoursGate
        from tender_search.client import SourceError
        with self.assertRaises(SourceError):WorkHoursGate().acquire()
        gate.return_value.acquire.assert_called_once()

    def test_fractional_hours_and_comma(self):
        self.client.force_login(self.owner)
        url=f'/profiles/{self.profile.pk}/notifications/'
        for hours,minutes in [('1',60),('2',120),('12',720)]:
            self.client.post(url,{'hours':hours,'action':'interval'})
            self.profile.refresh_from_db()
            self.assertEqual(self.profile.interval_minutes,minutes)
        self.assertEqual(self.profile.interval_hours,'12')
        for invalid in ['0','0.5','1.5','12.5','13','NaN']:
            self.client.post(url,{'hours':invalid,'action':'interval'})
            self.profile.refresh_from_db()
            self.assertEqual(self.profile.interval_minutes,720)

    def test_contacts_include_unlinked_starts_and_preserve_stop_status(self):
        from .models import TelegramContact
        chat={'id':6789,'type':'private','username':'subscriber_test','first_name':'Тест','last_name':'Подписчик'}
        handle_update({'message':{'chat':chat,'text':'/start'}})
        contact=TelegramContact.objects.get(chat_id=6789)
        self.assertEqual(contact.username,'subscriber_test')
        self.assertTrue(contact.enabled)
        self.assertTrue(contact.first_seen_at)
        self.assertFalse(TelegramSubscription.objects.filter(chat_id=6789).exists())
        handle_update({'message':{'chat':chat,'text':'/stop'}})
        handle_update({'message':{'chat':dict(chat,username='new_name'),'text':'Привет'}})
        contact.refresh_from_db()
        self.assertFalse(contact.enabled)
        self.assertEqual(contact.username,'new_name')
        handle_update({'message':{'chat':chat,'text':'/start'}})
        contact.refresh_from_db();self.assertTrue(contact.enabled)
        handle_update({'message':{'chat':{'id':9999,'type':'group'},'text':'/start'}})
        self.assertEqual(TelegramContact.objects.count(),1)

    def test_legacy_contact_gets_first_seen_on_actual_message_only(self):
        from .models import TelegramContact
        from .telegram_service import record_contact
        TelegramContact.objects.create(chat_id=6789,enabled=False)
        chat={'id':6789,'type':'private','username':'legacy_user'}
        record_contact(chat)
        contact=TelegramContact.objects.get(pk=6789)
        self.assertFalse(contact.enabled)
        self.assertIsNone(contact.first_seen_at)
        handle_update({'message':{'chat':chat,'text':'/start'}})
        contact.refresh_from_db()
        self.assertTrue(contact.first_seen_at)
        self.assertTrue(contact.enabled)
