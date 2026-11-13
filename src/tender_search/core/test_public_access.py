from datetime import timedelta
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.utils import timezone
from .access import access_for, eligible_user_ids
from .models import CompanySettings, SearchProfile, TrialAccess, TelegramSubscription


@override_settings(STORAGES={
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
})
class PublicAccessTests(TestCase):
    def signup(self, **changes):
        data = {'username': 'supplier', 'email': 'Buyer@Example.com',
                'password1': 'Example-strong-7192!', 'password2': 'Example-strong-7192!'}
        return self.client.post('/register/', dict(data, **changes))

    def test_public_pages_and_private_routes(self):
        for url in ('/', '/login/', '/register/'):
            self.assertEqual(self.client.get(url).status_code, 200)
        page = self.client.get('/')
        self.assertContains(page, 'Tender<span>Search</span>')
        self.assertContains(page, 'href="/register/"')
        self.assertContains(page, 'href="/login/"')
        for url in ('/main/', '/search/', '/workspace/', '/favorites/', '/administration/'):
            self.assertRedirects(self.client.get(url), '/login/?next='+url, fetch_redirect_response=False)

    def test_signup_starts_fourteen_days_and_logs_in_without_privileges(self):
        before = timezone.now()
        response = self.signup(is_superuser='true', is_staff='true', days='999')
        self.assertRedirects(response, '/account/email/')
        user = get_user_model().objects.get(username='supplier')
        self.assertFalse(user.is_staff or user.is_superuser)
        self.assertEqual(user.email, 'buyer@example.com')
        self.assertEqual(int(self.client.session['_auth_user_id']), user.pk)
        access = user.trial_access
        self.assertGreaterEqual(access.expires_at, before+timedelta(days=14))
        self.assertLessEqual(access.expires_at, timezone.now()+timedelta(days=14))
        company = CompanySettings.objects.get(owner=user)
        self.assertEqual((company.company_bin, company.company_name, company.bank_account), ('', '', ''))

    def test_unique_email_required_and_normalized(self):
        get_user_model().objects.create_user('existing', email='buyer@example.com')
        for email in ('', 'BUYER@example.com'):
            page = self.signup(email=email)
            self.assertEqual(page.status_code, 200)
            self.assertIn('email', page.context['form'].errors)
        self.assertEqual(get_user_model().objects.count(), 1)

    def test_password_validation_and_csrf(self):
        page = self.signup(password1='123', password2='123')
        self.assertIn('password2', page.context['form'].errors)
        self.assertFalse(get_user_model().objects.exists())
        self.assertEqual(Client(enforce_csrf_checks=True).post('/register/', {}).status_code, 403)

    def test_expiry_blocks_pages_writes_and_fragments_but_preserves_data(self):
        user = get_user_model().objects.create_user('expired')
        TrialAccess.objects.create(owner=user, expires_at=timezone.now()-timedelta(seconds=1))
        profile = SearchProfile.objects.create(owner=user, name='Моя подборка')
        self.client.force_login(user)
        for url in ('/main/', '/search/', '/settings/', '/workspace/', '/favorites/',
                    '/lots/1/', '/lots/1/documents/1/', '/settings/extension/download/'):
            self.assertRedirects(self.client.get(url), '/account/access/', fetch_redirect_response=False)
        self.assertRedirects(self.client.post(f'/profiles/{profile.pk}/delete/'), '/account/access/')
        self.assertTrue(SearchProfile.objects.filter(pk=profile.pk).exists())
        response = self.client.get('/search/', HTTP_HX_REQUEST='true')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response['HX-Redirect'], '/account/access/')
        self.assertContains(self.client.get('/account/access/'), 'Бесплатный период')
        self.assertEqual(self.client.get('/').status_code, 200)
        self.assertEqual(self.client.post('/logout/').status_code, 302)

    def test_boundary_is_exact_and_expired_missing_record_is_not_restarted(self):
        user = get_user_model().objects.create_user('old')
        now = timezone.now()
        get_user_model().objects.filter(pk=user.pk).update(date_joined=now-timedelta(days=20))
        user.refresh_from_db()
        self.assertLess(access_for(user).expires_at, now)
        TrialAccess.objects.filter(owner=user).update(expires_at=now)
        self.client.force_login(user)
        with patch('django.utils.timezone.now', return_value=now):
            self.assertRedirects(self.client.get('/main/'), '/account/access/', fetch_redirect_response=False)
            self.assertNotIn(user.pk, eligible_user_ids().values_list('pk', flat=True))

    def test_admin_can_edit_days_and_zero_blocks_immediately(self):
        admin = get_user_model().objects.create_superuser('admin', email='admin@example.com')
        user = get_user_model().objects.create_user('customer')
        self.client.force_login(admin)
        page = self.client.get('/administration/')
        self.assertContains(page, 'Осталось дней')
        self.assertContains(page, 'Без ограничений')
        url = f'/administration/users/{user.pk}/access/'
        self.assertRedirects(self.client.post(url, {'days': 30}), '/administration/')
        self.assertAlmostEqual((user.trial_access.expires_at-timezone.now()).total_seconds()/86400, 30, places=3)
        self.assertContains(self.client.get(f'/administration/users/{user.pk}/edit/'), 'Сохранить срок доступа')
        self.assertRedirects(self.client.post(url, {'days': 0}), '/administration/')
        self.client.force_login(user)
        self.assertRedirects(self.client.get('/main/'), '/account/access/', fetch_redirect_response=False)
        self.client.force_login(admin)
        self.client.post(url, {'days': 5})
        self.client.force_login(user)
        self.assertEqual(self.client.get('/main/').status_code, 200)

    def test_user_cannot_extend_access_and_admin_rejects_invalid_days(self):
        user = get_user_model().objects.create_user('customer')
        self.client.force_login(user)
        url = f'/administration/users/{user.pk}/access/'
        self.assertEqual(self.client.post(url, {'days': 999}).status_code, 403)
        admin = get_user_model().objects.create_superuser('admin', email='admin@example.com')
        self.client.force_login(admin)
        for days in (-1, 'abc', 3651, '1.5'):
            self.assertEqual(self.client.post(url, {'days': days}).status_code, 400)
        self.assertEqual(self.client.get(url).status_code, 405)
        self.assertEqual(Client(enforce_csrf_checks=True).post(url, {'days': 20}).status_code, 403)

    def test_account_changes_do_not_reset_access(self):
        user = get_user_model().objects.create_user('customer', email='c@example.com')
        expiry = timezone.now()+timedelta(days=2)
        TrialAccess.objects.create(owner=user, expires_at=expiry)
        admin = get_user_model().objects.create_superuser('admin', email='admin@example.com')
        self.client.force_login(admin)
        self.client.post(f'/administration/users/{user.pk}/edit/', {
            'username': user.username, 'email': user.email, 'role': 'user', 'is_active': 'on'})
        self.assertEqual(TrialAccess.objects.get(owner=user).expires_at, expiry)

    @override_settings(TELEGRAM_BOT_TOKEN='test-only')
    def test_expired_accounts_are_excluded_from_background_work(self):
        from .collections_service import enqueue_collection
        from .telegram_service import schedule_profiles
        from .status_monitor import schedule_status_checks

        user = get_user_model().objects.create_user('expired')
        TrialAccess.objects.create(owner=user, expires_at=timezone.now()-timedelta(days=1))
        profile = SearchProfile.objects.create(owner=user, name='Test', enabled=True, collection_enabled=True)
        TelegramSubscription.objects.create(owner=user, enabled=True, chat_id=12345)
        self.assertNotIn(user.pk, eligible_user_ids().values_list('pk', flat=True))
        with patch('tender_search.core.tasks.refresh_collection.delay') as collection:
            self.assertFalse(enqueue_collection(profile.pk, force=True))
            collection.assert_not_called()
        with patch('tender_search.core.telegram_service.in_work_hours', return_value=True), patch('tender_search.core.tasks.monitor_profile.delay') as monitor:
            schedule_profiles()
            monitor.assert_not_called()
        with patch('tender_search.core.status_monitor.in_work_hours', return_value=True):
            schedule_status_checks()


