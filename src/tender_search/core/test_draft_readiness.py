from django.test import TestCase,override_settings
from bs4 import BeautifulSoup
from . import test_draft_queue
from .models import ApplicationPreparation,CompanySettings
from .draft_readiness import readiness

@override_settings(STORAGES={'default':{'BACKEND':'django.core.files.storage.FileSystemStorage'},'staticfiles':{'BACKEND':'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class DraftReadinessTests(TestCase):
    def setUp(self):test_draft_queue.DraftQueueTests.setUp(self)

    def test_queue_reads_are_batched_and_do_not_build_hidden_workspace_page(self):
        from copy import deepcopy
        from unittest.mock import patch
        from django.test import RequestFactory
        from django.test.utils import CaptureQueriesContext
        from django.db import connection
        from .draft_queue import queue
        from .models import Lot, DraftQueueItem, UserLotWorkflow, CollectionLot
        original=ApplicationPreparation.objects.get(owner=self.owner,lot=self.lot)
        profile=CollectionLot.objects.filter(profile__owner=self.owner,lot=self.lot).first().profile
        request=RequestFactory().get('/workspace/drafts/')
        request.user=self.owner;request.session={}
        counts=[]
        for total in (1,20):
            for index in range(DraftQueueItem.objects.filter(owner=self.owner).count(),total):
                lot=deepcopy(self.lot);lot.pk=None;lot.source_id=1000+index;lot.number=f'{1000+index}-ЗЦП1'
                lot.raw['_pricing_detail']['id']=lot.source_id;lot.save()
                CollectionLot.objects.create(profile=profile,lot=lot,stage='preparing')
                UserLotWorkflow.objects.update_or_create(owner=self.owner,lot=lot,defaults={'stage':'preparing'})
                ApplicationPreparation.objects.create(owner=self.owner,lot=lot,company_bin=original.company_bin,data=original.data)
                DraftQueueItem.objects.create(owner=self.owner,lot=lot)
            with patch('tender_search.core.draft_queue.expire_draft_queue'), patch('tender_search.core.views.result_context') as hidden_page, CaptureQueriesContext(connection) as captured:
                response=queue(request)
            hidden_page.assert_not_called()
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.content.count(b'data-queue-id='),total)
            counts.append(len(captured))
        self.assertEqual(counts[0],counts[1])

    def test_registry_cache_miss_is_shared_by_readiness_and_pricing(self):
        from .lot_pricing import attach_lot_budgets
        from .preparation import draft_position_count
        from .models import AnnouncementLotCache
        self.lot.raw={};self.lot.save()
        AnnouncementLotCache.objects.all().delete()
        attach_lot_budgets([self.lot])
        with self.assertNumQueries(0):
            self.assertIsNone(draft_position_count(self.lot))
            self.assertIsNone(draft_position_count(self.lot))

    def row(self):
        page=self.client.get('/workspace/drafts/')
        return BeautifulSoup(page.content,'html.parser').select_one('[data-queue-id]')

    def test_missing_data_disables_draft_but_allows_preparation(self):
        ApplicationPreparation.objects.filter(owner=self.owner,lot=self.lot).delete()
        row=self.row()
        self.assertTrue(row.select_one('.queue-start').has_attr('disabled'))
        self.assertFalse(row.select_one('.queue-prepare').has_attr('aria-disabled'))
        self.assertIn('Заполните данные',row.get_text())
        self.assertEqual(self.client.post(self.base+'packet/',{'launch_guard':'1'}).status_code,400)

    def test_valid_data_enables_launch_and_missing_required_field_blocks_it(self):
        self.assertFalse(self.row().select_one('.queue-start').has_attr('disabled'))
        prep=ApplicationPreparation.objects.get(owner=self.owner,lot=self.lot)
        prep.data['bank_account']='';prep.save()
        self.assertTrue(self.row().select_one('.queue-start').has_attr('disabled'))
        self.assertEqual(self.client.post(self.base+'packet/',{'launch_guard':'1'}).status_code,400)

    def test_company_type_and_bin_are_checked(self):
        for changes in [{'taxpayer_type':''},{'company_bin':'123456789012'}]:
            CompanySettings.objects.filter(owner=self.owner).update(**changes)
            self.assertTrue(self.row().select_one('.queue-start').has_attr('disabled'))
            self.assertEqual(self.client.post(self.base+'packet/',{'launch_guard':'1'}).status_code,400)

    def test_filling_disables_all_row_actions_and_server_rejects_second_launch(self):
        for state in ['running','waiting']:
            self.item.state=state;self.item.save()
            row=self.row()
            self.assertTrue(row.select_one('.queue-start').has_attr('disabled'))
            self.assertEqual(row.select_one('.queue-prepare')['aria-disabled'],'true')
            self.assertEqual(self.client.post(f'/lots/{self.lot.pk}/preparation/',self.data).status_code,403)
            self.assertEqual(self.client.post(f'/lots/{self.lot.pk}/price/',{'unit_gross':'116'}).status_code,403)
            page=BeautifulSoup(self.client.get(f'/lots/{self.lot.pk}/preparation/').content,'html.parser')
            self.assertTrue(page.select_one('[name=unit_gross]').has_attr('disabled'))
            self.assertTrue(row.select_one('.queue-remove').has_attr('disabled'))
            self.assertEqual(self.client.post(self.base+'packet/',{'launch_guard':'1'}).status_code,400)
