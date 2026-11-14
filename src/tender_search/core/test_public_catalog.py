from datetime import timedelta

from bs4 import BeautifulSoup
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from .models import IndexedLot, TrialAccess
from .catalog_sections import CATEGORIES, REGIONS, active_section_paths


@override_settings(PUBLIC_SITE_URL='https://tender-search.kz',
    CACHES={'default': {'BACKEND': 'django.core.cache.backends.dummy.DummyCache'}},
    ALLOWED_HOSTS=['testserver', 'tender-search.kz'], STORAGES={
        'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
        'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
    })
class PublicCatalogTests(TestCase):
    def make_lot(self, pk=1, **overrides):
        values = dict(announcement_source_id=123, current=True, status_id=9,
                      deadline=timezone.now()+timedelta(days=2), row={
            'name_ru': 'Ноутбук для офиса', 'lot_number': f'{pk}-ЗЦП1',
            'description_ru': '<script>alert(1)</script>\nОписание товара',
            'total_price': '120000.00', 'customers': [{'name_ru': 'Заказчик'}],
            'delivery_addresses': [{'full_name_ru': 'г. Алматы'}],
            'purchase_method_name': 'Запрос ценовых предложений',
            'private_unknown_field': 'DO-NOT-PUBLISH-RAW-JSON',
        })
        values.update(overrides)
        return IndexedLot.objects.create(source_id=pk, **values)

    def get_public(self, path):
        return self.client.get(path, secure=True, HTTP_HOST='tender-search.kz')

    def test_anonymous_catalog_and_card_use_only_public_fields(self):
        lot = self.make_lot()
        catalog = self.get_public('/catalog/')
        self.assertContains(catalog, '/catalog/lots/1/')
        self.assertContains(catalog, 'href="/register/"')
        self.assertContains(catalog, 'Встроенная CRM')
        self.assertNotIn('X-Robots-Tag', catalog)
        response = self.get_public('/catalog/lots/1/?utm_source=test')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'https://zakup.gov.kz/announcement/123#lot-1')
        self.assertContains(response, '&lt;script&gt;alert(1)&lt;/script&gt;')
        self.assertNotContains(response, 'DO-NOT-PUBLISH-RAW-JSON')
        self.assertNotContains(response, '<script>alert(1)</script>')
        soup = BeautifulSoup(response.content, 'html.parser')
        self.assertEqual(soup.select_one('link[rel=canonical]')['href'], 'https://tender-search.kz/catalog/lots/1/')
        self.assertEqual(len(soup.select('h1')), 1)
        self.assertEqual(IndexedLot.objects.get(pk=lot.pk).row, lot.row)

    def test_closed_inactive_future_publication_and_expired_lots_are_not_public(self):
        self.make_lot(1, current=False)
        self.make_lot(2, deadline=timezone.now()-timedelta(seconds=1))
        self.make_lot(3, status_id=20)
        self.make_lot(4, status_id=6)
        page = self.get_public('/catalog/')
        self.assertContains(page, 'Пока нет лотов с открытым приёмом')
        for pk in (1, 2, 3, 4, 999):
            response = self.get_public(f'/catalog/lots/{pk}/')
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response['X-Robots-Tag'], 'noindex, nofollow')

    def test_pagination_has_stable_order_and_own_canonical(self):
        deadline = timezone.now()+timedelta(days=2)
        for pk in range(1, 27):
            self.make_lot(pk, deadline=deadline)
        first = self.get_public('/catalog/')
        self.assertEqual([card['id'] for card in first.context['lots']], list(range(1, 25)))
        second = self.get_public('/catalog/?page=2&utm_source=test')
        self.assertEqual([card['id'] for card in second.context['lots']], [25, 26])
        soup = BeautifulSoup(second.content, 'html.parser')
        self.assertEqual(soup.select_one('link[rel=canonical]')['href'], 'https://tender-search.kz/catalog/?page=2')
        for page in ('0', '-1', '3', 'abc', '1.5'):
            self.assertEqual(self.get_public('/catalog/?page='+page).status_code, 404)

    def test_preview_noindex_and_private_workspace_remains_protected(self):
        self.make_lot()
        for path in ('/catalog/', '/catalog/lots/1/'):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response['X-Robots-Tag'], 'noindex, nofollow')
        for path in ('/search/', '/workspace/', '/lots/1/', '/administration/'):
            self.assertEqual(self.get_public(path).status_code, 302)

    def test_expired_account_can_browse_public_catalog(self):
        user = get_user_model().objects.create_user(username='expired-catalog-reader')
        TrialAccess.objects.create(owner=user, expires_at=timezone.now()-timedelta(days=1))
        self.client.force_login(user)
        self.make_lot()
        self.assertEqual(self.get_public('/catalog/').status_code, 200)
        self.assertEqual(self.get_public('/catalog/lots/1/').status_code, 200)
        self.assertRedirects(self.get_public('/workspace/'), '/account/access/', fetch_redirect_response=False)

    def test_catalog_is_read_only_and_missing_amount_is_not_zero(self):
        self.make_lot(row={'name_ru': 'Товар без суммы', 'total_price': 'NaN'})
        self.assertContains(self.get_public('/catalog/lots/1/'), 'Не указана')
        for path in ('/catalog/', '/catalog/lots/1/'):
            self.assertEqual(self.client.post(path).status_code, 405)

    def test_regions_use_delivery_codes_not_customer_or_name(self):
        self.make_lot(1, row={'name_ru': 'Товар', 'delivery_addresses': [{'code': '751410000'}]})
        self.make_lot(2, row={'name_ru': 'Товар в Алматы', 'customers': [{'name_ru': 'Алматы'}],
                             'delivery_addresses': [{'code': '191010000'}]})
        self.make_lot(3, row={'name_ru': 'Без места поставки'})
        self.make_lot(4, row={'name_ru': 'Две поставки', 'delivery_addresses': [
            {'code': '751110000'}, {'code': '711110000'}]})
        self.make_lot(5, row={'name_ru': 'Неверный код', 'delivery_addresses': [{'code': '75BADCODE'}]})
        for slug, expected in [('almaty', {1, 4}), ('almatinskaya-oblast', {2}), ('astana', {4})]:
            response = self.get_public(f'/catalog/regions/{slug}/')
            self.assertEqual(response.status_code, 200)
            self.assertEqual({item['id'] for item in response.context['lots']}, expected)
        self.assertEqual(self.get_public('/catalog/').context['page'].paginator.count, 5)

    def test_categories_match_titles_not_customer_or_description(self):
        self.make_lot(1, row={'name_ru': 'Ноутбуки для школы'})
        self.make_lot(2, row={'name_ru': 'Столы офисные'})
        self.make_lot(3, row={'name_ru': 'Бумага', 'description_ru': 'Ноутбук', 'customers': [{'name_ru': 'Компьютер'}]})
        self.make_lot(4, row={'name_ru': 'Услуги столовой'})
        self.make_lot(5, row={'name': 'МОНИТОР'})
        response = self.get_public('/catalog/categories/kompyutery-i-orgtehnika/')
        self.assertEqual({item['id'] for item in response.context['lots']}, {1, 5})
        response = self.get_public('/catalog/categories/mebel/')
        self.assertEqual({item['id'] for item in response.context['lots']}, {2})

    def test_empty_sections_noindex_and_sitemap_only_contains_populated_sections(self):
        self.make_lot(row={'name_ru': 'Ноутбук', 'delivery_addresses': [{'code': '751410000'}]})
        paths = active_section_paths()
        self.assertCountEqual(paths, ['/catalog/categories/kompyutery-i-orgtehnika/', '/catalog/regions/almaty/'])
        sitemap = self.get_public('/sitemap.xml')
        for path in paths:
            self.assertContains(sitemap, 'https://tender-search.kz'+path)
            self.assertNotIn('X-Robots-Tag', self.get_public(path))
        empty_path = '/catalog/regions/astana/'
        self.assertNotContains(sitemap, 'https://tender-search.kz'+empty_path)
        empty = self.get_public(empty_path)
        self.assertEqual(empty.status_code, 200)
        self.assertEqual(empty['X-Robots-Tag'], 'noindex, nofollow')
        self.assertContains(empty, 'name="robots" content="noindex, nofollow"')
        self.make_lot(2, row={'name_ru': 'Товар', 'delivery_addresses': [{'code': '711110000'}]})
        self.assertNotIn('X-Robots-Tag', self.get_public(empty_path))
        self.assertIn(empty_path, active_section_paths())

    def test_section_pagination_keeps_filter_and_canonical(self):
        for pk in range(1, 26):
            self.make_lot(pk, row={'name_ru': 'Компьютер', 'delivery_addresses': [{'code': '751410000'}]})
        for path in ('/catalog/categories/kompyutery-i-orgtehnika/', '/catalog/regions/almaty/'):
            response = self.get_public(path+'?page=2&utm_source=ignored')
            self.assertEqual(len(response.context['lots']), 1)
            soup = BeautifulSoup(response.content, 'html.parser')
            self.assertEqual(soup.select_one('link[rel=canonical]')['href'], 'https://tender-search.kz'+path+'?page=2')
            self.assertEqual(soup.select_one('.catalog-pagination a')['href'], path+'#catalog-lots')
            self.assertEqual(self.get_public(path+'?page=3').status_code, 404)

    def test_curated_sections_have_unique_metadata_and_public_navigation(self):
        titles, descriptions = set(), set()
        root = BeautifulSoup(self.get_public('/catalog/').content, 'html.parser')
        for section in CATEGORIES + REGIONS:
            path = section['path']
            self.assertIsNotNone(root.select_one(f'a[href="{path}"]'))
            response = self.get_public(path+'?utm_source=test')
            self.assertEqual(response.status_code, 200)
            soup = BeautifulSoup(response.content, 'html.parser')
            titles.add(soup.title.text)
            descriptions.add(soup.select_one('meta[name=description]')['content'])
            self.assertEqual(soup.h1.text, section['heading'])
            self.assertEqual(soup.select_one('link[rel=canonical]')['href'], 'https://tender-search.kz'+path)
            self.assertEqual(self.client.post(path).status_code, 405)
        self.assertEqual(len(titles), 28)
        self.assertEqual(len(descriptions), 28)
        for path in ('/catalog/categories/nonexistent/', '/catalog/regions/nonexistent/'):
            self.assertEqual(self.get_public(path).status_code, 404)
