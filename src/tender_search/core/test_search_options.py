from decimal import Decimal
from types import SimpleNamespace
from django.test import TestCase, SimpleTestCase, override_settings
from django.contrib.auth import get_user_model
from bs4 import BeautifulSoup
from .forms import SearchForm
from .models import WebSearch, SearchHit, SearchProfile, CollectionLot
from .search_service import store_lot
from .test_index import row
from .lot_pricing import budget_unit_price
from tender_search.matching import PreparedMatcher


class UnitBudgetTests(SimpleTestCase):
    def test_single_position_and_missing_or_ambiguous_quantity(self):
        lot=SimpleNamespace(source_id=1,amount=Decimal('14000'),raw={'_pricing_detail':{
            'id':1,'is_combined':False,'plan_items':[{'count':4}]}})
        self.assertEqual(budget_unit_price(lot),Decimal('3500.00'))
        detail=lot.raw['_pricing_detail']
        for count in [0,None,'NaN','-1','0.00000001']:
            detail['plan_items']=[{'count':count}]
            self.assertIsNone(budget_unit_price(lot))
        detail['plan_items']=[{'count':1},{'count':3}]
        self.assertIsNone(budget_unit_price(lot))
        detail['plan_items']=[{'count':4}];detail['is_combined']=True
        self.assertIsNone(budget_unit_price(lot))
        detail['is_combined']=False;detail['id']=2
        self.assertIsNone(budget_unit_price(lot))


@override_settings(STORAGES={'default':{'BACKEND':'django.core.files.storage.FileSystemStorage'},
    'staticfiles':{'BACKEND':'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class SearchOptionTests(TestCase):
    def setUp(self):
        self.owner=get_user_model().objects.create_user('page-options')
        self.client.force_login(self.owner)
        self.job=WebSearch.objects.create(owner=self.owner,status='done',specification={'terms':['flash']})
        self.profile=SearchProfile.objects.create(owner=self.owner,name='Pages')
        for index in range(1,26):
            lot=store_lot(row(index,announcement_id=100+index))
            SearchHit.objects.create(search=self.job,lot=lot,terms=['flash'])
            CollectionLot.objects.create(profile=self.profile,lot=lot)
        self.url=f'/search/{self.job.pk}/'

    def test_page_sizes_and_session_are_scoped_and_keep_sort_stage_filters(self):
        response=self.client.get(self.url,{'per_page':'10','sort':'amount','remaining':'2'})
        self.assertEqual(response.context['page'].paginator.per_page,10)
        soup=BeautifulSoup(response.content,'html.parser')
        self.assertEqual([item['value'] for item in soup.select('select[name=per_page] option')],['10','20','50','100'])
        hidden={item['name']:item['value'] for item in soup.select('.page-size-controls input[type=hidden]')}
        self.assertEqual(hidden,{'sort':'amount'})
        self.assertEqual(self.client.get(self.url).context['per_page'],10)
        response=self.client.get('/workspace/',{'per_page':'50','stage':'all','profile':self.profile.pk,'sort':'-amount'})
        self.assertEqual(response.context['page'].paginator.per_page,50)
        self.assertEqual(self.client.get(self.url).context['per_page'],10)
        for invalid in ['0','-1','25','100000','invalid']:
            self.assertEqual(self.client.get('/workspace/',{'per_page':invalid}).context['per_page'],50)
        for size in [10,20,50,100]:
            response=self.client.get(self.url,{'per_page':size})
            self.assertEqual(len(response.context['page']),min(25,size))
        soup=BeautifulSoup(self.client.get(self.url).content,'html.parser')
        quantities=soup.select('.lot-quantity-cell')
        prices=soup.select('.budget-unit-cell')
        self.assertEqual(len(quantities),25)
        self.assertEqual(len(prices),25)
        self.assertEqual({cell.get_text(strip=True) for cell in quantities},{'Не указано'})
        self.assertEqual({cell.get_text(strip=True) for cell in prices},{'Не указана'})

    def test_phrase_form_preserves_words_and_searches_only_adjacent_prefixes(self):
        form=SearchForm({'terms':'беспроводн мыш; игров клавиатур','kind':'phrase','mode':'any','number_kind':'lot_number'})
        self.assertTrue(form.is_valid(),form.errors)
        self.assertEqual(form.specification()['terms'],['беспроводн мыш','игров клавиатур'])
        matcher=PreparedMatcher(form.specification())
        self.assertTrue(matcher('Беспроводная мышь',''))
        self.assertTrue(matcher('Радиус действия беспроводной мыши — 10 метров',''))
        strict=PreparedMatcher(dict(form.specification(),terms=['беспроводная мыш']))
        self.assertTrue(strict('Радиус действия беспроводной мыши — 10 метров',''))
        self.assertFalse(matcher('Беспроводной комплект клавиатура и мышь',''))
        self.assertFalse(matcher('Сверхбеспроводная мышь',''))
        self.assertFalse(matcher('Мышь беспроводная',''))
