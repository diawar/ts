from datetime import timedelta
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from .models import SearchProfile, TrialAccess


@override_settings(STORAGES={'default':{'BACKEND':'django.core.files.storage.FileSystemStorage'},
                            'staticfiles':{'BACKEND':'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class AccountIdentityTests(TestCase):
    def setUp(self):
        self.first=get_user_model().objects.create_user('identity-first')
        self.second=get_user_model().objects.create_user('identity-second')
        self.profile=SearchProfile.objects.create(owner=self.second,name='Second private collection')

    def test_identity_is_uncached_and_works_for_anonymous_or_expired_account(self):
        response=self.client.get('/account/identity/')
        self.assertEqual(response.json(),{'user_id':None})
        self.assertIn('no-store',response['Cache-Control'])
        self.assertIn('Cookie',response['Vary'])
        self.client.force_login(self.first)
        TrialAccess.objects.update_or_create(owner=self.first,defaults={'expires_at':timezone.now()-timedelta(days=1)})
        response=self.client.get('/account/identity/')
        self.assertEqual(response.json(),{'user_id':str(self.first.pk)})
        self.assertEqual(response['X-TenderSearch-User'],str(self.first.pk))

    def test_switching_shared_session_returns_new_identity_without_personal_fields(self):
        self.client.force_login(self.first)
        self.assertEqual(self.client.get('/account/identity/').json(),{'user_id':str(self.first.pk)})
        self.client.force_login(self.second)
        self.assertEqual(self.client.get('/account/identity/').json(),{'user_id':str(self.second.pk)})

    def test_stale_ajax_and_html_form_cannot_write_to_new_accounts_collection(self):
        self.client.force_login(self.second)
        endpoint=f'/profiles/{self.profile.pk}/rename/'
        response=self.client.post(endpoint,{'name':'Old account data'},HTTP_X_TENDERSEARCH_USER=str(self.first.pk))
        self.assertEqual(response.status_code,409)
        response=self.client.post(endpoint,{'name':'Old account data','_account_id':str(self.first.pk)})
        self.assertEqual(response.status_code,409)
        self.profile.refresh_from_db();self.assertEqual(self.profile.name,'Second private collection')
        self.assertEqual(self.client.post(endpoint,{'name':'Allowed'},HTTP_X_TENDERSEARCH_USER=str(self.second.pk)).status_code,200)

    def test_stale_get_is_refused_and_private_pages_load_account_guard_first(self):
        self.client.force_login(self.second)
        response=self.client.get('/search/',HTTP_X_TENDERSEARCH_USER=str(self.first.pk))
        self.assertEqual(response.status_code,409)
        self.assertNotContains(response,'Second private collection',status_code=409)
        response=self.client.get('/search/')
        self.assertContains(response,'core/private-pages.js')
        self.assertLess(response.content.index(b'core/private-pages.js'),response.content.index(b'core/search.js'))
