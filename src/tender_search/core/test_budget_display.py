from decimal import Decimal
from django.test import TestCase, override_settings
from django.template.loader import render_to_string
from .lot_pricing import attach_lot_budgets, lot_budget_display
from .models import Lot, SearchHit, WebSearch, Favorite


@override_settings(STORAGES={'default':{'BACKEND':'django.core.files.storage.FileSystemStorage'},'staticfiles':{'BACKEND':'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class BudgetDisplayTests(TestCase):
    def setUp(self):
        from .test_lot_pricing import LotPricingTests
        LotPricingTests.setUp(self)
        self.lot.amount=Decimal('112110');self.lot.save()
        self.detail['plan_items'][0].update(count=30,unit_price=3737)
        self.cache.rows=[self.detail];self.cache.save()

    def test_cached_cards_are_batched_and_template_never_queries(self):
        lots=list(Lot.objects.select_related('announcement'))
        with self.assertNumQueries(1):attach_lot_budgets(lots)
        with self.assertNumQueries(0):
            html=render_to_string('core/_budget_unit_price.html',{'lot':lots[0]})
        self.assertIn('30 шт.',html)
        self.assertIn('3 737,00',html)
        self.assertIn('без НДС',html)

    def test_snapshot_needs_no_cache_query_and_bad_counts_are_not_guessed(self):
        self.lot.raw['_pricing_detail']=self.detail
        with self.assertNumQueries(0):attach_lot_budgets([self.lot])
        self.assertEqual(lot_budget_display(self.lot)['quantity'],30)
        del self.lot._budget_display
        self.detail['plan_items'][0]['count']=0
        self.assertEqual(lot_budget_display(self.lot),{})
        self.detail['plan_items']*=2
        self.assertTrue(lot_budget_display(self.lot)['multiple'])

    def test_search_favorites_workspace_and_open_lot_show_same_numbers(self):
        Favorite.objects.get_or_create(owner=self.owner,lot=self.lot)
        job=WebSearch.objects.create(owner=self.owner,status='done',specification={'terms':['мыш']})
        SearchHit.objects.create(search=job,lot=self.lot,terms=['мыш'])
        for url in [f'/search/{job.pk}/','/favorites/','/workspace/?profile=all&stage=preparing',f'/lots/{self.lot.pk}/']:
            response=self.client.get(url, follow=True)
            self.assertContains(response,'Количество × бюджет за единицу')
            self.assertContains(response,'30 шт.')
            self.assertContains(response,'3 737,00')

    def test_all_lot_tables_have_separate_quantity_unit_price_and_amount(self):
        from bs4 import BeautifulSoup
        from .workflow_state import update_workflow
        Favorite.objects.get_or_create(owner=self.owner,lot=self.lot)
        job=WebSearch.objects.create(owner=self.owner,status='done',specification={'terms':['мыш']})
        SearchHit.objects.create(search=job,lot=self.lot,terms=['мыш'])
        for url,columns in [(f'/search/{job.pk}/',6),('/favorites/',7),('/workspace/?stage=all',6)]:
            with self.subTest(url=url):
                if url.startswith('/workspace/'):
                    update_workflow(self.owner.pk,self.lot.pk,stage='new')
                response=self.client.get(url)
                table=BeautifulSoup(response.content,'html.parser').select_one('.procurement-table')
                self.assertIsNotNone(table)
                self.assertEqual(len(table.select('thead th')),columns)
                self.assertEqual(len(table.select('tbody tr > td')),columns)
                self.assertIn('Кол-во',table.select('thead th')[1].get_text())
                self.assertIn('Цена за единицу',table.select('thead th')[2].get_text())
                self.assertIn('30 шт.',table.select_one('.lot-quantity-cell').get_text(' ',strip=True))
                self.assertIn('3 737,00',table.select_one('.budget-unit-cell').get_text(' ',strip=True))
                self.assertNotIn('30 шт.',table.select_one('.amount').get_text(' ',strip=True))

    def test_combined_lot_counts_external_plan_versions_once_and_keeps_bid_gate(self):
        from .lot_pricing import verified_pricing, price_budget
        self.lot.amount=Decimal('22000')
        self.detail.update(is_combined=True, system={'id':1}, plan_items=[
            {'id':10,'external_id':100,'count':1,'unit_price':5500,'measure':{'id':3525,'name':'Штука'}},
            {'id':11,'external_id':101,'count':3,'unit_price':5500,'measure':{'id':3525,'name':'Штука'}},
            {'id':12,'external_id':100,'count':1,'unit_price':5500,'measure':{'id':3525,'name':'Штука'}},
        ])
        self.lot.raw['_pricing_detail']=self.detail
        with self.assertNumQueries(0):attach_lot_budgets([self.lot])
        budget=lot_budget_display(self.lot)
        self.assertEqual((budget['quantity'],budget['unit'],budget['positions']),(4,5500,2))
        self.assertIsNone(verified_pricing(self.lot))
        self.assertIsNone(price_budget(self.lot))
        self.lot.save()
        Favorite.objects.get_or_create(owner=self.owner,lot=self.lot)
        from bs4 import BeautifulSoup
        response=self.client.get('/workspace/drafts/')
        table=BeautifulSoup(response.content,'html.parser').select_one('.work-lots-table')
        self.assertEqual(len(table.select('thead th')),7)
        row=table.select_one('tbody tr')
        self.assertEqual(len(row.select(':scope > td')),7)
        self.assertIn('4 шт.',row.select_one('.work-quantity').get_text(' ',strip=True))
        self.assertIn('5 500,00',row.select_one('.work-unit').get_text(' ',strip=True))
        self.assertIn('22 000,00',row.select_one('.work-amount').get_text(' ',strip=True))
        self.assertNotIn('22 000,00',row.select_one('.work-lot').get_text(' ',strip=True))

    def test_combined_counts_are_not_guessed_for_conflicts_or_mismatched_totals(self):
        from copy import deepcopy
        self.lot.amount=Decimal('22000')
        detail={'id':self.lot.source_id,'is_combined':True,'system':{'id':1},'plan_items':[
            {'id':10,'external_id':100,'count':1,'unit_price':5500,'measure':{'id':1,'name':'Штука'}},
            {'id':11,'external_id':101,'count':3,'unit_price':5500,'measure':{'id':1,'name':'Штука'}},
        ]}
        cases=[]
        bad=deepcopy(detail);bad['plan_items'][1]['count']=4;cases.append(bad)
        bad=deepcopy(detail);bad['plan_items'][1]['measure']={'id':2,'name':'Комплект'};cases.append(bad)
        bad=deepcopy(detail);bad['plan_items'].append({**bad['plan_items'][0],'count':2});cases.append(bad)
        bad=deepcopy(detail);bad['plan_items'][0]['count']='NaN';cases.append(bad)
        for value in cases:
            with self.subTest(detail=value):
                self.assertNotIn('quantity',lot_budget_display(self.lot,value))

    def test_combined_positions_with_different_prices_do_not_show_an_average(self):
        detail={'id':self.lot.source_id,'is_combined':True,'system':{'id':1},'plan_items':[
            {'id':10,'count':1,'unit_price':100,'measure':{'id':1,'name':'Штука'}},
            {'id':11,'count':2,'unit_price':200,'measure':{'id':1,'name':'Штука'}},
        ]}
        self.lot.amount=Decimal('500')
        budget=lot_budget_display(self.lot,detail)
        self.assertEqual(budget['quantity'],3)
        self.assertIsNone(budget['unit'])
        self.assertTrue(budget['varied_units'])
