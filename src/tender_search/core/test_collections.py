from .test_status_watch import set_watch
from datetime import timedelta
from unittest.mock import patch
from bs4 import BeautifulSoup
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from .models import (SearchProfile, CollectionLot, Favorite, AnnouncementWatch, ProcurementIndex,
                     MedicalIndex, IndexedLot, WebSearch, SearchHit, TelegramDelivery)
from .collections_service import enqueue_collection, schedule_collections, update_collection
from .test_index import row, put
from .search_service import store_lot


@override_settings(STORAGES={'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
                            'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class CollectionsTests(TestCase):
    def setUp(self):
        refresh=patch('tender_search.core.tasks.refresh_lot.delay')
        refresh.start();self.addCleanup(refresh.stop)
        self.owner = get_user_model().objects.create_user('collection-owner')
        self.other = get_user_model().objects.create_user('other-collection-owner')
        self.client.force_login(self.owner)
        self.spec = {'terms': ['flash'], 'exclude': [], 'kind': 'contains', 'mode': 'any',
                     'filters': {'status_id__in': '6__7__9'}, 'status_selection': ['open:9']}
        self.profile = SearchProfile.objects.create(owner=self.owner, name='Накопители',
            filters={'specification': self.spec}, collection_lease='lease')
        ProcurementIndex.objects.create(completed_at=timezone.now())
        for source in ('med_ecc', 'fms_ecc'):
            MedicalIndex.objects.create(source=source, completed_at=timezone.now())
        put(row(1))

    def run_refresh(self):
        SearchProfile.objects.filter(pk=self.profile.pk).update(collection_lease='lease')
        self.profile.refresh_from_db()
        update_collection(self.profile.pk, 'lease', self.profile.collection_revision)

    def test_manual_storage_is_hidden_but_aggregate_keeps_its_lots(self):
        from .workflow_state import add_to_participation
        self.profile.delete()
        lot=store_lot(row(99,name_ru='Лот, добавленный вручную'))
        add_to_participation(self.owner.pk,lot.pk)
        manual=SearchProfile.objects.get(owner=self.owner,filters__manual=True)
        response=self.client.get('/workspace/drafts/')
        self.assertEqual(response.status_code,200)
        self.assertEqual([item.lot_id for item in response.context['items']],[lot.pk])
        tabs=BeautifulSoup(response.content,'html.parser').select('.collection-tabs a')
        self.assertEqual([tab.get_text(' ',strip=True) for tab in tabs],['Общая подборка'])
        self.assertNotContains(response,'Пока нет подборок')
        self.assertNotContains(response,manual.name)
        self.assertEqual(next(step['count'] for step in response.context['steps'] if step['key']=='preparing'),1)
        self.assertEqual(list(self.client.get('/search/').context['profiles']),[])
        self.assertEqual(self.client.get('/main/').context['profiles'],0)
        self.assertTrue(CollectionLot.objects.filter(profile=manual,lot=lot).exists())
        self.client.force_login(self.other)
        self.assertNotContains(self.client.get('/workspace/drafts/'),lot.name)
        self.assertEqual(self.client.get('/workspace/',{'profile':manual.pk}).status_code,404)

    def test_manual_links_open_aggregate_without_losing_stage_or_sort(self):
        from .workflow_state import manual_profile
        manual=manual_profile(self.owner.pk)
        for path in ['/workspace/','/workspace/drafts/']:
            response=self.client.get(path,{'profile':manual.pk,'stage':'payment','sort':'-amount','per_page':'50'})
            self.assertRedirects(response,path+'?profile=all&stage=payment&sort=-amount&per_page=50',fetch_redirect_response=False)
        response=self.client.get('/workspace/')
        tabs=BeautifulSoup(response.content,'html.parser').select('.collection-tabs a')
        self.assertEqual([tab.get_text(' ',strip=True) for tab in tabs],['Общая подборка','Накопители'])
        self.assertEqual(list(self.client.get('/search/').context['profiles']),[self.profile])

    def test_refresh_uses_local_filter_without_telegram_and_preserves_workflow(self):
        with patch('tender_search.client.ZakupClient.search', side_effect=AssertionError('No network')):
            self.run_refresh()
        item = CollectionLot.objects.get(profile=self.profile)
        self.assertEqual(item.lot.source_id, 1)
        self.assertFalse(WebSearch.objects.exists())
        item.stage, item.outcome = 'delivery', 'won'
        item.save()
        self.run_refresh()
        item.refresh_from_db()
        self.assertEqual((item.stage, item.outcome), ('delivery', 'won'))
        self.assertEqual(CollectionLot.objects.count(), 1)
        self.profile.refresh_from_db()
        self.assertTrue(self.profile.collection_updated_at)
        self.assertEqual(self.profile.collection_lease, '')

    def test_missing_results_leave_active_work_and_stars_visible(self):
        put(row(2)); put(row(3))
        self.run_refresh()
        work = CollectionLot.objects.get(lot__source_id=1)
        work.stage='payment'; work.save()
        starred = CollectionLot.objects.get(lot__source_id=2)
        starred.stage='preparing';starred.save()
        Favorite.objects.get_or_create(owner=self.owner, lot=starred.lot)[0]
        IndexedLot.objects.update(current=False)
        self.run_refresh()
        self.assertFalse(CollectionLot.objects.filter(matches_filter=True).exists())
        response = self.client.get('/workspace/', {'profile':self.profile.pk})
        self.assertEqual(response.context['page'].paginator.count, 0)
        payment = self.client.get('/workspace/', {'profile':self.profile.pk, 'stage':'payment'})
        self.assertEqual([item.lot.source_id for item in payment.context['page']], [1])
        interesting = self.client.get('/workspace/', {'profile':self.profile.pk, 'stage':'preparing'}, follow=True)
        self.assertEqual([item.lot.source_id for item in interesting.context['items']], [2])

    def test_partial_refresh_adds_new_matches_without_erasing_old_membership(self):
        self.run_refresh()
        IndexedLot.objects.update(current=False)
        put(row(2, _details_pending=True))
        self.run_refresh()
        self.assertEqual(CollectionLot.objects.filter(matches_filter=True).count(), 2)

    def test_important_historical_lots_from_same_profile_are_recovered(self):
        lot=store_lot(row(20))
        Favorite.objects.get_or_create(owner=self.owner, lot=lot)[0]
        old=WebSearch.objects.create(owner=self.owner, specification=dict(self.spec, profile_id=self.profile.pk))
        SearchHit.objects.create(search=old, lot=lot, terms=['flash'])
        self.run_refresh()
        self.assertTrue(CollectionLot.objects.filter(profile=self.profile, lot=lot, matches_filter=False).exists())
        self.assertTrue(WebSearch.objects.filter(pk=old.pk).exists())

    def test_revision_fence_rejects_old_filter_results_and_keeps_new_lease(self):
        from .index_service import execute_local_search
        def supersede(job_id):
            execute_local_search(job_id)
            SearchProfile.objects.filter(pk=self.profile.pk).update(collection_revision=2,collection_lease='new-lease')
        with patch('tender_search.core.index_service.execute_local_search', side_effect=supersede):
            self.run_refresh()
        self.assertFalse(CollectionLot.objects.exists())
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.collection_lease,'new-lease')
        self.assertFalse(WebSearch.objects.exists())

    def test_failure_preserves_old_results_and_releases_lease(self):
        self.run_refresh()
        with patch('tender_search.core.index_service.execute_local_search', side_effect=RuntimeError()):
            self.run_refresh()
        self.assertEqual(CollectionLot.objects.count(),1)
        self.profile.refresh_from_db()
        self.assertTrue(self.profile.collection_error)
        self.assertEqual(self.profile.collection_lease,'')

    @patch('tender_search.core.tasks.refresh_collection.delay')
    def test_scheduler_works_without_bot_and_skips_paused_or_inactive_owners(self, task):
        SearchProfile.objects.filter(pk=self.profile.pk).update(collection_lease='', collection_lease_until=None)
        with override_settings(TELEGRAM_BOT_TOKEN=''), patch('tender_search.core.telegram_service.in_work_hours',return_value=False):
            schedule_collections()
        task.assert_called_once()
        self.assertFalse(enqueue_collection(self.profile.pk,force=True))
        self.profile.refresh_from_db()
        self.profile.collection_enabled=False;self.profile.collection_lease_until=None;self.profile.save()
        task.reset_mock();schedule_collections();task.assert_not_called()
        self.owner.is_active=False;self.owner.save()
        self.assertFalse(enqueue_collection(self.profile.pk,force=True))

    @patch('tender_search.core.tasks.refresh_collection.delay',side_effect=RuntimeError())
    def test_enqueue_failure_is_recoverable(self, task):
        self.assertFalse(enqueue_collection(self.profile.pk,force=True))
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.collection_lease,'')
        self.assertTrue(self.profile.collection_error)

    def test_tabs_owner_isolation_and_selected_star_and_plane(self):
        self.run_refresh()
        SearchProfile.objects.create(owner=self.owner,name='Вторая')
        foreign=SearchProfile.objects.create(owner=self.other,name='Чужая')
        item=CollectionLot.objects.get()
        Favorite.objects.get_or_create(owner=self.owner,lot=item.lot)[0]
        set_watch(owner=self.owner,lot=item.lot,announcement=item.lot.announcement,enabled=True)
        response=self.client.get('/workspace/',{'profile':self.profile.pk,'stage':'preparing'},follow=True)
        soup=BeautifulSoup(response.content,'html.parser')
        self.assertEqual(len(soup.select('.collection-tab')),3)
        self.assertEqual(len(soup.select('.funnel-step')),12)
        self.assertTrue(soup.select_one('.favorite-icon.selected'))
        self.assertTrue(soup.select_one('.status-watch-icon.selected'))
        self.assertNotContains(response,'Чужая')
        self.assertEqual(self.client.get('/workspace/',{'profile':foreign.pk}).status_code,404)
        self.assertEqual(self.client.get('/workspace/',{'profile':'bad'}).status_code,404)
        self.client.force_login(self.other)
        self.assertEqual(self.client.post(f'/workspace/items/{item.pk}/stage/',{'stage':'payment'}).status_code,404)
        self.assertEqual(self.client.post(f'/profiles/{self.profile.pk}/refresh-collection/').status_code,404)
        self.assertEqual(self.client.post(f'/profiles/{self.profile.pk}/tracking/').status_code,404)

    def test_stage_and_outcome_are_manual_validated_and_do_not_change_other_profile(self):
        self.run_refresh()
        item=CollectionLot.objects.get()
        other=SearchProfile.objects.create(owner=self.owner,name='Другая')
        independent=CollectionLot.objects.create(profile=other,lot=item.lot)
        endpoint=f'/workspace/items/{item.pk}/stage/'
        self.assertEqual(self.client.get(endpoint).status_code,405)
        self.client.post(endpoint,{'stage':'decision','outcome':'won'})
        item.refresh_from_db();independent.refresh_from_db()
        self.assertEqual((item.stage,item.outcome),('new',''))
        self.assertEqual(independent.stage,'new')
        self.client.post(endpoint,{'stage':'preparing','outcome':'won'})
        item.refresh_from_db();self.assertEqual((item.stage,item.outcome),('preparing',''))
        self.client.post(endpoint,{'stage':'hacked','outcome':'lost'})
        item.refresh_from_db();self.assertEqual(item.outcome,'')

    def test_tracking_toggle_enables_telegram_and_validates_interval_without_requiring_chat(self):
        endpoint=f'/profiles/{self.profile.pk}/tracking/'
        self.profile.collection_enabled=False;self.profile.save()
        with patch('tender_search.core.tasks.refresh_collection.delay'), self.captureOnCommitCallbacks(execute=True):
            self.client.post(endpoint,{'action':'toggle','hours':'2'},HTTP_HX_REQUEST='true')
        self.profile.refresh_from_db()
        self.assertTrue(self.profile.collection_enabled);self.assertTrue(self.profile.enabled)
        self.assertEqual(self.profile.interval_minutes,120)
        self.client.post(endpoint,{'action':'interval','hours':'1.5'})
        self.profile.refresh_from_db();self.assertEqual(self.profile.interval_minutes,120)
        self.client.post(endpoint,{'action':'toggle','hours':'2'})
        self.profile.refresh_from_db();self.assertFalse(self.profile.enabled)

    def test_saving_current_filter_starts_collection_and_telegram_and_invalidates_old_job(self):
        from .models import TelegramSubscription
        TelegramSubscription.objects.create(owner=self.owner, enabled=True, chat_id=555123)
        values={'terms':'flash','mode':'any','kind':'contains','number_kind':'lot_number','action':'save'}
        with patch('tender_search.core.tasks.refresh_collection.delay') as enqueue, self.captureOnCommitCallbacks(execute=True):
            response=self.client.post('/search/',values)
        self.assertEqual(response.status_code,302);enqueue.assert_called_once()
        saved=SearchProfile.objects.exclude(pk=self.profile.pk).get()
        self.assertTrue(saved.enabled);self.assertTrue(saved.collection_enabled)
        previous=saved.collection_revision
        with patch('tender_search.core.tasks.refresh_collection.delay'), self.captureOnCommitCallbacks(execute=True):
            self.client.post('/search/',dict(values,profile_id=saved.pk,terms='клавиатур'))
        saved.refresh_from_db();self.assertEqual(saved.collection_revision,previous+1)

    def test_saved_controls_use_tracking_eye_and_sidebar_has_collections(self):
        response=self.client.get('/search/')
        soup=BeautifulSoup(response.content,'html.parser')
        self.assertTrue(soup.select_one('.sidebar a[href="/workspace/"]'))
        self.assertTrue(soup.select_one('.saved-list .profile-tracking use')['href'].endswith('#eye'))
        self.assertFalse(soup.select('.saved-list .status-watch-icon'))

    def test_personal_notes_are_shared_between_own_collections_but_hidden_from_others(self):
        from .models import UserLotNote
        self.run_refresh()
        item=CollectionLot.objects.get()
        own_second=SearchProfile.objects.create(owner=self.owner,name='Моя вторая')
        CollectionLot.objects.create(profile=own_second,lot=item.lot)
        foreign=SearchProfile.objects.create(owner=self.other,name='Другой пользователь')
        foreign_item=CollectionLot.objects.create(profile=foreign,lot=item.lot)
        self.client.post(f'/lots/{item.lot_id}/note/',
            {'note':'Мой контакт\n<script>alert(1)</script>'})
        note=UserLotNote.objects.get(owner=self.owner,lot=item.lot)
        self.assertIn('\n',note.text)
        response=self.client.get(f'/lots/{item.lot_id}/')
        self.assertContains(response,'Мой контакт')
        self.assertNotContains(response,'<script>alert(1)</script>')
        self.assertContains(response,'&lt;script&gt;alert(1)&lt;/script&gt;')
        self.client.force_login(self.other)
        response=self.client.get('/workspace/',{'profile':foreign.pk})
        self.assertNotContains(response,'Мой контакт')
        self.client.post(f'/lots/{foreign_item.lot_id}/note/',
            {'note':'Другая личная заметка'})
        note.refresh_from_db();self.assertIn('Мой контакт',note.text)
        self.assertEqual(UserLotNote.objects.filter(lot=item.lot).count(),2)

    def test_note_edit_preserves_current_view_and_rejects_overlong_text_atomically(self):
        from .models import UserLotNote
        self.run_refresh(); item=CollectionLot.objects.get()
        item.stage='delivery';item.save()
        url=f'/lots/{item.lot_id}/note/'
        response=self.client.post(url,{'note':'Доставить утром','return_to':'/workspace/?stage=preparing'})
        self.assertIn('stage=preparing',response.url)
        self.client.post(url,{'stage':'payment','outcome':'lost','note':'x'*10001})
        item.refresh_from_db()
        self.assertEqual(item.stage,'delivery')
        self.assertEqual(UserLotNote.objects.get().text,'Доставить утром')
        self.profile.delete()
        self.assertEqual(UserLotNote.objects.get().text,'Доставить утром')

    def test_personal_state_is_identical_in_all_views_after_each_action(self):
        from .models import TelegramSubscription
        self.run_refresh()
        item = CollectionLot.objects.get()
        lot = item.lot
        lot.refresh_state = 'done'
        lot.checked_at = timezone.now()
        lot.save()
        TelegramSubscription.objects.create(owner=self.owner, chat_id=987654, enabled=True)
        job = WebSearch.objects.create(owner=self.owner, specification=self.spec, status='done', complete=True)
        SearchHit.objects.create(search=job, lot=lot, terms=['flash'])
        urls = [f'/search/{job.pk}/?view=table', f'/search/{job.pk}/?view=cards',
                f'/lots/{lot.pk}/', f'/workspace/?profile={self.profile.pk}']

        def check(star, plane):
            for url in urls + (['/favorites/'] if star else []):
                with self.subTest(url=url, star=star, plane=plane):
                    if url.startswith('/workspace/'):
                        item.refresh_from_db()
                        url += '&stage=' + ('all' if item.stage == 'new' else item.stage)
                    response = self.client.get(url, follow=True)
                    self.assertEqual(response.status_code, 200)
                    actions = BeautifulSoup(response.content, 'html.parser').select_one(f'.lot-actions[data-lot-id="{lot.pk}"]')
                    self.assertIsNotNone(actions)
                    self.assertEqual(actions.select_one('.favorite-icon')['aria-pressed'], str(star).lower())
                    self.assertEqual(actions.select_one('.status-watch-icon')['aria-pressed'], str(plane).lower())
                    self.assertIsNone(actions.select_one('.collection-membership'))
            state = self.client.get('/lots/ui-state/', {'ids': lot.pk}).json()
            self.assertEqual(state['lots'], [{'id': str(lot.pk), 'favorite': star, 'watched': plane, 'saved': True}])

        check(False, False)
        self.client.post(f'/lots/{lot.pk}/favorite/', {'action': 'add'}, HTTP_HX_REQUEST='true')
        check(True, True)
        self.client.post(f'/lots/{lot.pk}/status-watch/', {'action': 'on'}, HTTP_HX_REQUEST='true')
        check(True, True)
        self.client.post(f'/lots/{lot.pk}/status-watch/', {'action': 'off'}, HTTP_HX_REQUEST='true')
        check(True, True)
        self.client.post(f'/lots/{lot.pk}/status-watch/', {'action': 'on'}, HTTP_HX_REQUEST='true')
        self.client.post(f'/lots/{lot.pk}/favorite/', {'action': 'remove'}, HTTP_HX_REQUEST='true')
        check(False, False)

    def test_ui_state_and_saved_badge_are_private_and_follow_visible_membership(self):
        self.run_refresh()
        item = CollectionLot.objects.get()
        lot = item.lot
        Favorite.objects.get_or_create(owner=self.owner, lot=lot)[0]
        set_watch(owner=self.owner, announcement=lot.announcement, lot=lot, enabled=True)
        self.client.force_login(self.other)
        response = self.client.get('/lots/ui-state/', {'ids': lot.pk, 'owner': self.owner.pk})
        self.assertEqual(response['Cache-Control'], 'private, no-store')
        self.assertEqual(response.json()['lots'], [{'id': str(lot.pk), 'favorite': False, 'watched': False, 'saved': False}])
        self.client.force_login(self.owner)
        another = SearchProfile.objects.create(owner=self.owner, name='Вторая')
        CollectionLot.objects.create(profile=another, lot=lot)
        self.profile.delete()
        self.assertTrue(self.client.get('/lots/ui-state/', {'ids': lot.pk}).json()['lots'][0]['saved'])
        another.delete()
        self.assertFalse(self.client.get('/lots/ui-state/', {'ids': lot.pk}).json()['lots'][0]['saved'])
        self.assertEqual(self.client.get('/lots/ui-state/', {'ids': 'bad'}).status_code, 400)
        self.assertEqual(self.client.get('/lots/ui-state/', {'ids': ','.join(['1']*101)}).status_code, 400)
        self.client.logout()
        self.assertEqual(self.client.get('/lots/ui-state/', {'ids': lot.pk}).status_code, 302)

    def test_saving_requires_own_active_telegram_for_create_and_update(self):
        from .models import TelegramSubscription
        values={'terms':'flash','mode':'any','kind':'contains','number_kind':'lot_number','action':'save'}
        original=self.profile.filters.copy()
        def reject():
            for payload in (values, dict(values,profile_id=self.profile.pk,terms='changed')):
                response=self.client.post('/search/',payload)
                self.assertEqual(response.status_code,200)
                self.assertContains(response,'Для сохранения подборки подключите Telegram')
                soup=BeautifulSoup(response.content,'html.parser')
                self.assertTrue(soup.select_one('button[value="save"]').has_attr('disabled'))
            self.profile.refresh_from_db()
            self.assertEqual(self.profile.filters,original)
            self.assertEqual(SearchProfile.objects.filter(owner=self.owner).count(),1)
            self.assertFalse(WebSearch.objects.exists())
        TelegramSubscription.objects.create(owner=self.other,enabled=True,chat_id=555001)
        with patch('tender_search.core.tasks.refresh_collection.delay') as queued:
            reject()
            sub=TelegramSubscription.objects.create(owner=self.owner,enabled=True)
            reject()
            sub.chat_id=555002;sub.enabled=False;sub.save()
            reject()
            queued.assert_not_called()
        sub.enabled=True;sub.save()
        page=self.client.get('/search/')
        self.assertFalse(BeautifulSoup(page.content,'html.parser').select_one('button[value="save"]').has_attr('disabled'))

    def test_aggregate_collection_deduplicates_lots_with_shared_workflow(self):
        self.run_refresh()
        first=CollectionLot.objects.get()
        first.stage='applied';first.save()
        second=SearchProfile.objects.create(owner=self.owner,name='Вторая')
        duplicate=CollectionLot.objects.create(profile=second,lot=first.lot,stage='payment')
        unique=store_lot(row(21))
        CollectionLot.objects.create(profile=second,lot=unique)
        foreign=SearchProfile.objects.create(owner=self.other,name='Чужая')
        CollectionLot.objects.create(profile=foreign,lot=store_lot(row(22)))
        response=self.client.get('/workspace/')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.context['page'].paginator.count,1)
        self.assertTrue(response.context['aggregate_collection'])
        self.assertNotContains(response,'Чужая')
        self.assertEqual({s['key']:s['count'] for s in response.context['steps']}['all'],1)
        soup=BeautifulSoup(response.content,'html.parser')
        self.assertEqual(len(soup.select('.collection-stage-form')),0)
        self.assertEqual(len(soup.select('.lot-actions')),1)
        self.assertEqual(soup.select_one('#sidebar-collection-count').get_text(strip=True),'(2)')
        self.assertEqual(self.client.get('/lots/ui-state/',{'ids':first.lot_id}).json()['collection_count'],2)
        filtered=self.client.get('/workspace/',{'profile':'all','stage':'applied'})
        self.assertEqual(filtered.context['page'].paginator.count,1)
        self.assertEqual(len(filtered.context['page'][0].workflows),2)
        result=self.client.post(f'/workspace/items/{duplicate.pk}/stage/',{'stage':'contract','outcome':'won','return_scope':'all','return_stage':'all','note':'Общая заметка'})
        self.assertRedirects(result,'/workspace/?profile=all&stage=all')
        first.refresh_from_db();duplicate.refresh_from_db()
        self.assertEqual(first.stage,'applied')
        self.assertEqual(duplicate.stage,'applied')
        personal=self.client.get('/workspace/',{'profile':self.profile.pk})
        self.assertEqual(personal.context['page'].paginator.count,0)

    def test_lot_card_note_is_shared_private_and_does_not_change_star_or_plane(self):
        from .models import UserLotNote
        self.run_refresh()
        item=CollectionLot.objects.get()
        lot=item.lot;lot.refresh_state='done';lot.checked_at=timezone.now();lot.save()
        endpoint=f'/lots/{lot.pk}/note/'
        self.assertEqual(self.client.get(endpoint).status_code,405)
        result=self.client.post(endpoint,{'note':'<script>Моя заметка</script>','next':f'/workspace/?profile={self.profile.pk}'})
        self.assertRedirects(result,f'/lots/{lot.pk}/?next=%2Fworkspace%2F%3Fprofile%3D{self.profile.pk}')
        card=self.client.get(f'/lots/{lot.pk}/')
        self.assertContains(card,'&lt;script&gt;Моя заметка&lt;/script&gt;')
        self.assertEqual(card.context['lot_note'],'<script>Моя заметка</script>')
        collection=self.client.get('/workspace/',{'profile':self.profile.pk})
        self.assertEqual(collection.context['page'][0].note_text,card.context['lot_note'])
        self.assertFalse(Favorite.objects.filter(owner=self.owner).exists())
        self.assertFalse(AnnouncementWatch.objects.filter(owner=self.owner).exists())
        self.client.post(endpoint,{'note':'x'*10001})
        self.assertEqual(UserLotNote.objects.get(owner=self.owner,lot=lot).text,'<script>Моя заметка</script>')
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(f'/lots/{lot.pk}/').context['lot_note'],'')
        self.client.post(endpoint,{'note':'Другая заметка'})
        self.assertEqual(UserLotNote.objects.get(owner=self.owner,lot=lot).text,'<script>Моя заметка</script>')
        self.client.logout()
        self.assertEqual(self.client.post(endpoint,{'note':'Запрещено'}).status_code,302)

    def test_collection_sorting_amount_start_end_and_nulls_preserves_scope(self):
        from datetime import datetime,timezone as utc
        lots=[]
        for identifier,amount,start,end in [(31,300,'2026-11-02T08:00:00Z',4),(32,100,'2026-11-03T08:00:00Z',2),(33,200,'2026-11-01T08:00:00Z',3),(34,None,'',None)]:
            lot=store_lot(row(identifier,announcement_id=100+identifier,total_price=amount,offer_start_date=start))
            lot.announcement.deadline=datetime(2026,11,end,tzinfo=utc.utc) if end else None
            lot.announcement.save();lots.append(lot)
            CollectionLot.objects.create(profile=self.profile,lot=lot,stage='applied')
        duplicate=SearchProfile.objects.create(owner=self.owner,name='Дубль')
        CollectionLot.objects.create(profile=duplicate,lot=lots[0],stage='applied')
        expected={'amount':[32,33,31,34],'-amount':[31,33,32,34],'start':[33,31,32,34],'-start':[32,31,33,34],'end':[32,33,31,34],'-end':[31,33,32,34]}
        for scope in ('all',str(self.profile.pk)):
            for ordering,identifiers in expected.items():
                page=self.client.get('/workspace/',{'profile':scope,'stage':'applied','sort':ordering})
                self.assertEqual([item.lot.source_id for item in page.context['page']],identifiers)
                self.assertEqual(page.context['collection_sort'],ordering)
                self.assertContains(page,'sort=')
        item=CollectionLot.objects.filter(profile=self.profile,lot=lots[0]).get()
        moved=self.client.post(f'/workspace/items/{item.pk}/stage/',{'stage':'payment','outcome':'','return_scope':'all','return_sort':'-amount'})
        self.assertEqual(moved.url,'/workspace/?profile=all&stage=all&sort=-amount')
        item.refresh_from_db();self.assertEqual(item.stage,'applied')

    def test_stage_picker_and_inline_notes_hidden_all_stages_and_sort_links_work(self):
        self.run_refresh();item=CollectionLot.objects.get()
        for scope in ('all',str(self.profile.pk)):
            for stage in ('new','preparing','applied','won','lost','not_participated','contract','delivery','payment'):
                item.stage=stage;item.outcome=stage if stage in ('won','lost') else '';item.save()
                response=self.client.get('/workspace/',{'profile':scope,'stage':'all' if stage=='new' else stage},follow=True)
                soup=BeautifulSoup(response.content,'html.parser')
                self.assertFalse(soup.select('.collection-stage-form, .collection-note-form'))
                self.assertNotContains(response,'<th>Этап работы</th>',html=True)
                links=soup.select('thead a')
                self.assertEqual(len(links),3)
                for link in links:
                    self.assertNotIn('hx-target',link.attrs)
                    self.assertEqual(self.client.get('/workspace/'+link['href'],follow=True).status_code,200)
        response=self.client.post(f'/lots/{item.lot_id}/note/',{'note':'Личная','return_to':'https://evil.example/workspace/'})
        self.assertEqual(response.url,f'/lots/{item.lot_id}/')
