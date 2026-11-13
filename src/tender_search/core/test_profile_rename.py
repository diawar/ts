from datetime import timedelta
from unittest.mock import patch

from bs4 import BeautifulSoup
from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from .models import SearchProfile, TelegramSubscription, WebSearch


@override_settings(STORAGES={
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
})
class ProfileRenameTests(TestCase):
    def setUp(self):
        self.owner=get_user_model().objects.create_user('rename-owner')
        self.other=get_user_model().objects.create_user('rename-other')
        self.client.force_login(self.owner)
        now=timezone.now()
        self.profile=SearchProfile.objects.create(owner=self.owner, name='Техника',
            include_terms=['ноутбук'], exclude_terms=['ремонт'], enabled=True,
            filters={'form': {'terms': 'ноутбук'}, 'specification': {'terms': ['ноутбук']}},
            baseline_at=now, index_baseline_at=now, next_monitor_at=now,
            monitor_lease='active-monitor', monitor_lease_until=now+timedelta(minutes=5),
            collection_lease='active-collection', collection_lease_until=now+timedelta(minutes=5))
        self.url=f'/profiles/{self.profile.pk}/rename/'

    def test_rename_does_not_change_filters_monitor_state_or_start_search(self):
        before=SearchProfile.objects.values().get(pk=self.profile.pk)
        with patch('tender_search.core.tasks.web_search.delay') as search, \
                patch('tender_search.core.tasks.refresh_collection.delay') as refresh:
            response=self.client.post(self.url, {'name': '  Офисная техника  '})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'name': 'Офисная техника'})
        after=SearchProfile.objects.values().get(pk=self.profile.pk)
        for field in before.keys()-{'name', 'updated_at'}:
            self.assertEqual(after[field], before[field], field)
        self.assertEqual(after['name'], 'Офисная техника')
        search.assert_not_called(); refresh.assert_not_called()
        self.assertFalse(WebSearch.objects.exists())

    def test_only_owner_can_rename_and_only_post_is_allowed(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)
        self.client.force_login(self.other)
        self.assertEqual(self.client.post(self.url, {'name': 'Чужая'}).status_code, 404)
        self.client.logout()
        self.assertEqual(self.client.post(self.url, {'name': 'Без входа'}).status_code, 302)
        self.profile.refresh_from_db(); self.assertEqual(self.profile.name, 'Техника')

    def test_blank_or_overlong_names_are_rejected_without_changing_profile(self):
        for data in ({}, {'name': ''}, {'name': '  \t '}, {'name': 'а'*201}):
            response=self.client.post(self.url, data)
            self.assertEqual(response.status_code, 400)
            self.assertTrue(response.json()['error'])
            self.profile.refresh_from_db(); self.assertEqual(self.profile.name, 'Техника')
        self.assertEqual(self.client.post(self.url, {'name': 'а'*200}).status_code, 200)

    def test_csrf_is_required_and_valid_token_works(self):
        client=Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        self.assertEqual(client.post(self.url, {'name': 'Новое'}).status_code, 403)
        self.assertEqual(client.get('/search/').status_code, 200)
        token=client.cookies['csrftoken'].value
        self.assertEqual(client.post(self.url, {'name': 'Новое'}, HTTP_X_CSRFTOKEN=token).status_code, 200)

    def test_name_is_escaped_in_link_input_and_accessibility_labels(self):
        name='<img src=x onerror=alert(1)> "Техника"'
        self.client.post(self.url, {'name': name})
        response=self.client.get('/search/')
        soup=BeautifulSoup(response.content, 'html.parser')
        item=soup.select_one('[data-saved-profile]')
        self.assertEqual(item.select_one('[data-profile-label]').text, name)
        self.assertEqual(item.select_one('input[name="name"]')['value'], name)
        self.assertFalse(item.select('img'))

    def test_editor_exists_on_search_and_result_pages(self):
        job=WebSearch.objects.create(owner=self.owner, status='done', specification={'terms': ['ноутбук']})
        for path in ('/search/', f'/search/{job.pk}/'):
            response=self.client.get(path)
            soup=BeautifulSoup(response.content, 'html.parser')
            item=soup.select_one('[data-saved-profile]')
            self.assertIsNotNone(item.select_one('.saved-profile-folder'))
            self.assertIsNotNone(item.select_one('.saved-profile-pencil'))
            self.assertEqual(item.select_one('[data-profile-rename]')['action'], self.url)
            self.assertEqual(len(item.select('[data-profile-rename] button')), 2)

    def test_editing_saved_filters_preserves_custom_name(self):
        self.client.post(self.url, {'name': 'Моя техника'})
        TelegramSubscription.objects.create(owner=self.owner, enabled=True, chat_id=123)
        data={'profile_id': self.profile.pk, 'action': 'save', 'terms': 'планшет',
              'mode': 'any', 'kind': 'contains', 'number_kind': 'lot_number'}
        with patch('tender_search.core.collections_service.enqueue_collection'), self.captureOnCommitCallbacks(execute=True):
            response=self.client.post('/search/', data)
        self.assertEqual(response.status_code, 302)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.name, 'Моя техника')
        self.assertEqual(self.profile.include_terms, ['планшет'])
