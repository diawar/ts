import json
from xml.etree import ElementTree as ET
from bs4 import BeautifulSoup
from django.test import TestCase, override_settings

from .guides import GUIDES


@override_settings(PUBLIC_SITE_URL='https://tender-search.kz',
    CACHES={'default': {'BACKEND': 'django.core.cache.backends.dummy.DummyCache'}},
    ALLOWED_HOSTS=['testserver', 'tender-search.kz'], STORAGES={
        'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
        'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
    })
class SeoGuidesTests(TestCase):
    def production_get(self, path):
        return self.client.get(path, secure=True, HTTP_HOST='tender-search.kz')

    def test_landing_metadata_and_public_links(self):
        response = self.production_get('/?utm_source=test')
        self.assertEqual(response.status_code, 200)
        soup = BeautifulSoup(response.content, 'html.parser')
        self.assertEqual(soup.select_one('link[rel=canonical]')['href'], 'https://tender-search.kz/')
        self.assertEqual(soup.select_one('meta[name=robots]')['content'], 'index, follow')
        self.assertNotIn('X-Robots-Tag', response)
        self.assertIn('тендеров', soup.title.text)
        self.assertTrue(soup.select('a[href="/guides/"]'))
        self.assertEqual(len(soup.select('#guides .guide-card')), 3)
        self.assertIsNone(soup.select_one('meta[name=keywords]'))

    def test_preview_stays_noindex_but_points_to_future_domain(self):
        response = self.client.get('/')
        self.assertEqual(response['X-Robots-Tag'], 'noindex, nofollow')
        soup = BeautifulSoup(response.content, 'html.parser')
        self.assertEqual(soup.select_one('meta[name=robots]')['content'], 'noindex, nofollow')
        self.assertEqual(soup.select_one('link[rel=canonical]')['href'], 'https://tender-search.kz/')

    def test_robots_and_sitemap_work_without_login(self):
        robots = self.production_get('/robots.txt')
        self.assertContains(robots, 'Sitemap: https://tender-search.kz/sitemap.xml')
        self.assertTrue(robots['Content-Type'].startswith('text/plain'))
        response = self.production_get('/sitemap.xml')
        self.assertEqual(response.status_code, 200)
        doc = ET.fromstring(response.content)
        locations = [node.text for node in doc.iter('{http://www.sitemaps.org/schemas/sitemap/0.9}loc')]
        expected = ['https://tender-search.kz/', 'https://tender-search.kz/catalog/', 'https://tender-search.kz/guides/']
        expected += ['https://tender-search.kz'+article['path'] for article in GUIDES]
        self.assertCountEqual(locations, expected)
        self.assertEqual(len(locations), len(set(locations)))

    def test_all_articles_are_public_unique_and_have_valid_structured_data(self):
        titles, descriptions = set(), set()
        for article in GUIDES:
            with self.subTest(article=article['slug']):
                response = self.production_get(article['path']+'?utm_source=test')
                self.assertEqual(response.status_code, 200)
                self.assertNotIn('X-Robots-Tag', response)
                soup = BeautifulSoup(response.content, 'html.parser')
                titles.add(soup.title.text)
                descriptions.add(soup.select_one('meta[name=description]')['content'])
                self.assertEqual(soup.select_one('link[rel=canonical]')['href'], 'https://tender-search.kz'+article['path'])
                self.assertEqual(len(soup.select('h1')), 1)
                self.assertEqual(len(soup.select('.article-body section')), len(article['sections']))
                for link in soup.select('.article-toc a[href^="#"]'):
                    self.assertIsNotNone(soup.find(id=link['href'][1:]))
                graph = json.loads(soup.select_one('script[type="application/ld+json"]').string)['@graph']
                structured = next(item for item in graph if item['@type'] == 'Article')
                self.assertEqual(structured['headline'], article['title'])
                self.assertEqual(structured['datePublished'], article['published_at'].isoformat())
                self.assertTrue(soup.select('.related-guides a'))
                if article.get('legal'):
                    self.assertTrue(soup.select('.article-date-note'))
                    self.assertTrue(soup.select('.article-source a'))
        self.assertEqual(len(titles), len(GUIDES))
        self.assertEqual(len(descriptions), len(GUIDES))

    def test_catalog_and_unknown_article(self):
        response = self.production_get('/guides/')
        soup = BeautifulSoup(response.content, 'html.parser')
        self.assertEqual(len(soup.select('.guide-card')), len(GUIDES))
        response = self.production_get('/guides/missing-article/')
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response['X-Robots-Tag'], 'noindex, nofollow')

    def test_account_pages_and_protected_redirects_stay_noindex(self):
        for path in ('/login/', '/register/', '/main/', '/administration/'):
            response = self.production_get(path)
            self.assertEqual(response['X-Robots-Tag'], 'noindex, nofollow')
        self.assertEqual(self.production_get('/main/').status_code, 302)

    @override_settings(PUBLIC_SITE_URL='')
    def test_unconfigured_origin_uses_request_host_without_query(self):
        response = self.client.get('/guides/?x=1')
        soup = BeautifulSoup(response.content, 'html.parser')
        self.assertEqual(soup.select_one('link[rel=canonical]')['href'], 'http://testserver/guides/')

