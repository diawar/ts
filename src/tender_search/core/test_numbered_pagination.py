from urllib.parse import parse_qs, urlsplit
from django.core.paginator import Paginator
from django.test import SimpleTestCase, RequestFactory
from django.template.loader import render_to_string
from .templatetags.search_tags import numbered_pagination


class NumberedPaginationTests(SimpleTestCase):
    def test_numbers_current_page_and_long_ranges(self):
        context={'request':RequestFactory().get('/search/job/'),'per_page':20}
        page=Paginator(range(20000),20).page(500)
        data=numbered_pagination(context,page,'search-results')
        self.assertEqual([item['number'] for item in data['items'] if not item.get('ellipsis')],[1,498,499,500,501,502,1000])
        html=render_to_string('core/_numbered_pagination.html',data)
        self.assertIn('aria-current="page"',html)
        self.assertIn('Страница 499',html)
        self.assertEqual(sum(item.get('current',False) for item in data['items']),1)

    def test_links_preserve_filters_sort_scope_and_search_anchor(self):
        request=RequestFactory().get('/workspace/?profile=4&stage=payment&sort=-amount&region=1&region=2&page=2')
        data=numbered_pagination({'request':request,'per_page':50},Paginator(range(300),50).page(2),'search-results')
        url=urlsplit(data['next']);query=parse_qs(url.query)
        self.assertEqual(query,{'profile':['4'],'stage':['payment'],'sort':['-amount'],'region':['1','2'],'page':['3'],'per_page':['50']})
        self.assertEqual(url.fragment,'search-results')
        self.assertEqual(url.path,'/workspace/')

    def test_small_ranges_and_public_first_page_have_clean_urls(self):
        request=RequestFactory().get('/catalog/?page=2&utm_source=ignored')
        data=numbered_pagination({'request':request},Paginator(range(60),20).page(2),'catalog-lots',True)
        self.assertEqual(data['previous'],'/catalog/#catalog-lots')
        self.assertEqual(data['next'],'/catalog/?page=3#catalog-lots')
        self.assertEqual([item['number'] for item in data['items']],[1,2,3])
        self.assertEqual(numbered_pagination({'request':request},Paginator([],20).page(1)),{})
