"""Guard page-sized work and avoid loading extracted files just to show links."""
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, override_settings, RequestFactory
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from .models import AnnouncementLotCache, Document, DocumentVersion, Favorite, Lot, SearchProfile, CollectionLot
from .search_service import store_lot
from .test_index import row
from .lot_pricing import attach_lot_budgets, verified_pricing, price_budget, price_entry_data


@override_settings(STORAGES={'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
                            'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class PagePerformanceTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user('page-owner')
        self.client.force_login(self.owner)
        self.lot = store_lot(row(1, system_id=1, purchase_method_id=12))
        self.lot.refresh_state = 'done'
        self.lot.checked_at = timezone.now()
        self.lot.save()

    def test_workspace_poll_supplies_menu_counts_without_inherited_mutation_policy(self):
        from bs4 import BeautifulSoup
        profile=SearchProfile.objects.create(owner=self.owner,name='Workspace')
        CollectionLot.objects.create(profile=profile,lot=self.lot)
        Favorite.objects.create(owner=self.owner,lot=self.lot)
        response=self.client.get('/workspace/',HTTP_HX_REQUEST='true')
        root=BeautifulSoup(response.content,'html.parser').select_one('#collection-workspace')
        self.assertEqual(root['data-collection-count'],'1')
        self.assertEqual(root['data-favorite-count'],'1')
        self.assertTrue(root.has_attr('data-background-poll'))
        self.assertFalse(root.has_attr('hx-sync'))

    def test_personal_control_poll_has_constant_work_for_one_hundred_lots(self):
        from .views import lot_ui_state
        lots=Lot.objects.bulk_create([Lot(announcement=self.lot.announcement,source_id=i,name=f'Item {i}') for i in range(100,200)])
        profile=SearchProfile.objects.create(owner=self.owner,name='Own')
        CollectionLot.objects.bulk_create([CollectionLot(profile=profile,lot=lot,stage='preparing') for lot in lots])
        Favorite.objects.create(owner=self.owner,lot=lots[0])
        other=get_user_model().objects.create_user('other-page-owner')
        Favorite.objects.create(owner=other,lot=lots[1])
        factory=RequestFactory()
        for ids in ([lots[0].pk], [lot.pk for lot in lots]):
            request=factory.get('/lots/ui-state/',{'ids':','.join(map(str,ids))})
            request.user=self.owner
            with self.assertNumQueries(7), CaptureQueriesContext(connection) as captured:
                response=lot_ui_state(request)
            import json
            state=json.loads(response.content)
            self.assertEqual(len(state['lots']),len(ids))
            self.assertEqual(sum(item['favorite'] for item in state['lots']),1)
            self.assertEqual(state['collection_count'],100)
            for query in captured.captured_queries:
                self.assertNotIn('core_announcementlotcache',query['sql'])
                self.assertNotIn('"raw"',query['sql'])

    def test_page_card_lookup_is_shared_by_all_price_calculations(self):
        detail={'id':self.lot.source_id,'system':{'id':1},'is_combined':False,
                'plan_items':[{'count':3,'unit_price':'100','system':{'id':1},'purchase_method':{'id':12}}]}
        AnnouncementLotCache.objects.create(pk=self.lot.announcement.source_id,rows=[detail])
        with self.assertNumQueries(1):
            attach_lot_budgets([self.lot])
        with self.assertNumQueries(0):
            self.assertEqual(verified_pricing(self.lot)['quantity'],3)
            self.assertEqual(price_entry_data(self.lot)['quantity'],3)
            self.assertEqual(price_budget(self.lot)['unit'],100)
        # A missing cache entry is also reused, rather than queried repeatedly.
        AnnouncementLotCache.objects.all().delete()
        attach_lot_budgets([self.lot])
        with self.assertNumQueries(0):
            self.assertIsNone(verified_pricing(self.lot))
            self.assertIsNone(price_entry_data(self.lot))

    def test_document_links_use_latest_version_without_loading_extracted_text(self):
        Favorite.objects.create(owner=self.owner,lot=self.lot)
        doc=Document.objects.create(announcement=self.lot.announcement,lot=self.lot,
                                    source_id='technical',name='Specification')
        old=DocumentVersion.objects.create(document=doc,sha256='a'*64,text='old '*1000,pages=[{'text':'old'}])
        latest=DocumentVersion.objects.create(document=doc,sha256='b'*64,text='latest '*1000,pages=[{'text':'latest'}])
        with CaptureQueriesContext(connection) as captured, patch('tender_search.core.views.queue_refresh'):
            response=self.client.get(f'/lots/{self.lot.pk}/')
        self.assertContains(response,f'/lots/{self.lot.pk}/documents/{latest.pk}/')
        self.assertNotContains(response,f'/lots/{self.lot.pk}/documents/{old.pk}/')
        for query in captured.captured_queries:
            if 'core_documentversion' in query['sql']:
                self.assertNotIn('"text"',query['sql'])
                self.assertNotIn('"search_text"',query['sql'])
                self.assertNotIn('"pages"',query['sql'])
