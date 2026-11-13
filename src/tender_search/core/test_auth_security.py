from datetime import timedelta
import logging
import re
from unittest.mock import patch
from urllib.parse import urlsplit

from django.contrib.auth import get_user_model
from django.core import mail, signing
from django.core.management import call_command
from django.test import Client, RequestFactory, TestCase, override_settings
from django.utils import timezone

from .access import eligible_user_ids
from .auth_logging import RedactAuthLinks
from .auth_mail import (VERIFY_SALT, auth_origin, cleanup, deliver_one, message_for,
                        queue_reset, queue_verification, verification_record)
from .auth_security import client_ip, consume_limits
from .models import AuthMailJob, AuthRateBucket, EmailVerification, TrialAccess


@override_settings(
    AUTH_SITE_URL='https://accounts.example.test', AUTH_TRUST_CLOUDFLARE_IP=False,
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
    STORAGES={'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
              'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}},
)
class AuthSecurityTests(TestCase):
    password = 'Original-example-7192!'
    new_password = 'New-example-long-8431!'

    def setUp(self):
        self.user = get_user_model().objects.create_user('supplier', email='buyer@example.com', password=self.password)

    def mail_path(self, job):
        body = message_for(job)[1]
        url = re.search(r'https://\S+', body).group()
        self.assertTrue(url.startswith('https://accounts.example.test/'))
        return urlsplit(url).path

    def verification(self):
        EmailVerification.objects.create(owner=self.user, email=self.user.email)
        queue_verification(self.user)
        return self.mail_path(AuthMailJob.objects.latest('pk'))

    def test_new_signup_is_gated_and_has_no_background_access(self):
        response = self.client.post('/register/', {'username': 'new-supplier', 'email': 'New@example.com',
            'password1': self.password, 'password2': self.password})
        self.assertRedirects(response, '/account/email/')
        user = get_user_model().objects.get(username='new-supplier')
        self.assertTrue(user.email_verification.required)
        self.assertEqual(AuthMailJob.objects.get(owner=user).kind, 'verify')
        for path in ('/main/', '/search/', '/workspace/', '/settings/', '/account/access/'):
            self.assertRedirects(self.client.get(path), '/account/email/')
        page = self.client.post('/lots/1/favorite/', HTTP_HX_REQUEST='true')
        self.assertEqual(page.status_code, 403)
        self.assertEqual(page['HX-Redirect'], '/account/email/')
        self.assertNotIn(user.pk, eligible_user_ids().values_list('pk', flat=True))
        self.assertIn(self.user.pk, eligible_user_ids().values_list('pk', flat=True))

    def test_confirmation_get_does_not_consume_post_unlocks_once_without_extending_trial(self):
        path = self.verification()
        access = TrialAccess.objects.create(owner=self.user)
        original_expiry = access.expires_at
        self.client.force_login(self.user)
        self.assertContains(self.client.get(path), 'Подтвердить email')
        self.assertIsNone(EmailVerification.objects.get(owner=self.user).verified_at)
        self.assertContains(self.client.post(path), 'Email подтверждён')
        self.assertContains(self.client.post(path), 'Ссылка недействительна')
        self.assertEqual(self.client.get('/main/').status_code, 200)
        access.refresh_from_db()
        self.assertEqual(access.expires_at, original_expiry)
        self.assertIn(self.user.pk, eligible_user_ids().values_list('pk', flat=True))

    def test_anonymous_confirmation_does_not_log_in(self):
        self.assertContains(self.client.post(self.verification()), 'Email подтверждён')
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_confirmation_expired_tampered_inactive_and_changed_email(self):
        path = self.verification()
        token = path.rstrip('/').split('/')[-1]
        self.assertIsNotNone(verification_record(token))
        self.assertIsNone(verification_record(token+'broken'))
        with patch('django.core.signing.time.time', return_value=timezone.now().timestamp()+86401):
            self.assertIsNone(verification_record(token))
        self.user.is_active = False
        self.user.save()
        self.assertIsNone(verification_record(token))
        self.user.is_active = True
        self.user.email = 'changed@example.com'
        self.user.save()
        self.assertIsNone(verification_record(token))

    def test_resend_invalidates_old_link_and_is_authenticated_post_only(self):
        path = self.verification()
        self.assertEqual(self.client.post('/account/email/resend/').status_code, 302)
        self.client.force_login(self.user)
        self.assertEqual(self.client.get('/account/email/resend/').status_code, 405)
        self.assertRedirects(self.client.post('/account/email/resend/'), '/account/email/')
        self.assertContains(self.client.get(path), 'Ссылка недействительна')
        self.assertIsNone(message_for(AuthMailJob.objects.earliest('pk')))
        self.assertContains(self.client.post(self.mail_path(AuthMailJob.objects.latest('pk'))), 'Email подтверждён')
        count = AuthMailJob.objects.count()
        self.client.post('/account/email/resend/')
        self.assertEqual(AuthMailJob.objects.count(), count)

    def test_changed_confirmed_email_blocks_access_and_background_again(self):
        self.client.post(self.verification())
        self.user.email = 'different@example.com'
        self.user.save()
        self.client.force_login(self.user)
        self.assertRedirects(self.client.get('/main/'), '/account/email/')
        self.assertNotIn(self.user.pk, eligible_user_ids().values_list('pk', flat=True))

    def test_reset_unknown_and_inactive_have_same_response_and_no_mail(self):
        self.user.is_active = False
        self.user.save()
        responses = [self.client.post('/password/reset/', {'email': email}, follow=True)
                     for email in ('absent@example.com', self.user.email)]
        self.assertEqual(responses[0].content, responses[1].content)
        self.assertEqual(AuthMailJob.objects.count(), 2)
        # TestCase owns an outer transaction; a daemon's connection cleanup would close it.
        with patch('tender_search.core.management.commands.send_auth_mail.close_old_connections'):
            call_command('send_auth_mail', once=True)
        self.assertEqual(len(mail.outbox), 0)
        self.assertFalse(AuthMailJob.objects.filter(completed_at__isnull=True).exists())

    def test_reset_for_unusable_password_sends_nothing(self):
        self.user.set_unusable_password()
        self.user.save()
        queue_reset(self.user.email)
        deliver_one()
        self.assertEqual(len(mail.outbox), 0)

    def test_password_reset_changes_password_invalidates_token_and_other_sessions(self):
        other = Client()
        self.assertTrue(other.login(username=self.user.username, password=self.password))
        self.client.post('/password/reset/', {'email': 'BUYER@EXAMPLE.COM'})
        path = self.mail_path(AuthMailJob.objects.get())
        page = self.client.get(path, follow=True)
        self.assertTrue(page.context['validlink'])
        protected_path = page.request['PATH_INFO']
        self.assertNotEqual(protected_path, path)
        weak = self.client.post(protected_path, {'new_password1': '123', 'new_password2': '123'})
        self.assertTrue(weak.context['form'].errors)
        response = self.client.post(protected_path, {'new_password1': self.new_password, 'new_password2': self.new_password})
        self.assertRedirects(response, '/password/complete/')
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(self.new_password))
        self.assertNotIn('_auth_user_id', self.client.session)
        self.assertEqual(other.get('/main/').status_code, 302)
        self.assertNotIn('_auth_user_id', other.session)
        self.assertFalse(Client().get(path, follow=True).context['validlink'])

    def test_reset_expired_and_invalid_links(self):
        queue_reset(self.user.email)
        path = self.mail_path(AuthMailJob.objects.get())
        with override_settings(PASSWORD_RESET_TIMEOUT=-1):
            self.assertFalse(self.client.get(path, follow=True).context['validlink'])
        self.assertFalse(self.client.get('/password/reset/invalid/broken/').context['validlink'])

    def test_login_shared_throttle_and_no_forwarded_header_bypass(self):
        for _ in range(5):
            response = Client().post('/login/', {'username': self.user.username, 'password': 'wrong'},
                                     HTTP_X_FORWARDED_FOR='1.2.3.4')
            self.assertEqual(response.status_code, 200)
        response = Client().post('/login/', {'username': self.user.username, 'password': self.password},
                                 HTTP_X_FORWARDED_FOR='8.8.8.8', HTTP_CF_CONNECTING_IP='9.9.9.9')
        self.assertEqual(response.status_code, 429)
        self.assertIn('Retry-After', response)
        self.assertNotIn(self.user.username, ''.join(AuthRateBucket.objects.values_list('key', flat=True)))
        AuthRateBucket.objects.update(started_at=timezone.now()-timedelta(hours=2))
        self.assertEqual(self.client.post('/login/', {'username': self.user.username, 'password': self.password}).status_code, 302)

    def test_register_reset_resend_have_limits(self):
        for _ in range(3):
            self.assertEqual(self.client.post('/password/reset/', {'email': self.user.email}).status_code, 302)
        self.assertEqual(self.client.post('/password/reset/', {'email': self.user.email}).status_code, 429)
        for _ in range(5):
            self.assertEqual(self.client.post('/register/', {}).status_code, 200)
        self.assertEqual(self.client.post('/register/', {}).status_code, 429)
        self.client.force_login(self.user)
        for _ in range(3):
            self.assertEqual(self.client.post('/account/email/resend/').status_code, 302)
        self.assertEqual(self.client.post('/account/email/resend/').status_code, 429)

    def test_csrf_all_auth_writes_and_private_response_headers(self):
        client = Client(enforce_csrf_checks=True)
        verify = self.verification()
        queue_reset(self.user.email)
        reset = self.mail_path(AuthMailJob.objects.latest('pk'))
        protected = client.get(reset, follow=True).request['PATH_INFO']
        for path in ('/login/', '/register/', '/password/reset/', verify, protected):
            self.assertEqual(client.post(path, {}).status_code, 403, path)
        client.force_login(self.user)
        self.assertEqual(client.post('/account/email/resend/').status_code, 403)
        for path in ('/login/', '/register/', '/password/reset/', verify, protected, '/account/email/'):
            page = client.get(path)
            self.assertIn('no-store', page['Cache-Control'])
            self.assertEqual(page['Referrer-Policy'], 'strict-origin')
            self.assertEqual(page['X-Robots-Tag'], 'noindex, nofollow')

    def test_login_next_cannot_redirect_outside_site(self):
        response = self.client.post('/login/', {'username': self.user.username,
            'password': self.password, 'next': 'https://evil.example/'})
        self.assertEqual(response.url, '/main/')

    def test_authenticated_personal_pages_are_not_cached_after_logout(self):
        self.client.force_login(self.user)
        for path in ('/main/','/workspace/','/favorites/','/settings/'):
            response=self.client.get(path)
            self.assertEqual(response.status_code,200)
            self.assertIn('no-store',response['Cache-Control'])

    def test_browser_form_login_with_csrf_for_two_accounts_http_and_https(self):
        second = get_user_model().objects.create_user('second-supplier', email='second@example.com', password=self.password)
        for secure in (False, True):
            origin = ('https' if secure else 'http') + '://testserver'
            for user in (self.user, second):
                with self.subTest(secure=secure, username=user.username):
                    client = Client(enforce_csrf_checks=True)
                    page = client.get('/login/', secure=secure)
                    self.assertEqual(page['Referrer-Policy'], 'strict-origin')
                    token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page.content.decode()).group(1)
                    response = client.post('/login/', {'username': user.username, 'password': self.password,
                        'csrfmiddlewaretoken': token}, secure=secure, HTTP_ORIGIN=origin, HTTP_REFERER=origin+'/')
                    self.assertRedirects(response, '/main/', fetch_redirect_response=False)
                    self.assertEqual(int(client.session['_auth_user_id']), user.pk)
                    self.assertEqual(client.get('/main/', secure=secure).status_code, 200)
            # HTTPS browsers without Origin must still pass with origin-only Referer.
            if secure:
                client = Client(enforce_csrf_checks=True)
                page = client.get('/login/', secure=True)
                token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page.content.decode()).group(1)
                response = client.post('/login/', {'username': second.username, 'password': self.password,
                    'csrfmiddlewaretoken': token}, secure=True, HTTP_REFERER=origin+'/')
                self.assertEqual(response.status_code, 302)

    def test_login_rejects_untrusted_or_null_origin_even_with_valid_token(self):
        client = Client(enforce_csrf_checks=True)
        page = client.get('/login/')
        token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page.content.decode()).group(1)
        for origin in ('null', 'https://evil.example'):
            response = client.post('/login/', {'username': self.user.username, 'password': self.password,
                'csrfmiddlewaretoken': token}, HTTP_ORIGIN=origin)
            self.assertEqual(response.status_code, 403)
            self.assertNotIn('_auth_user_id', client.session)

    def test_auth_origin_does_not_use_host_or_seo_domain(self):
        with override_settings(ALLOWED_HOSTS=['evil.example'], PUBLIC_SITE_URL='https://future.example'):
            self.client.post('/password/reset/', {'email': self.user.email}, HTTP_HOST='evil.example')
        self.assertEqual(AuthMailJob.objects.get().origin, 'https://accounts.example.test')
        with override_settings(AUTH_SITE_URL='https://safe.example@evil.example/'):
            with self.assertRaisesMessage(Exception, 'trusted http(s) origin'):
                auth_origin()

    def test_mail_retry_recovery_scrubs_completed_job(self):
        queue_reset(self.user.email)
        with patch('tender_search.core.auth_mail.send_mail', side_effect=OSError('secret details')):
            self.assertTrue(deliver_one())
        job = AuthMailJob.objects.get()
        self.assertEqual((job.attempts, job.last_error), (1, 'OSError'))
        self.assertIsNone(job.completed_at)
        self.assertFalse(deliver_one())
        AuthMailJob.objects.update(available_at=timezone.now())
        self.assertTrue(deliver_one())
        job.refresh_from_db()
        self.assertIsNotNone(job.completed_at)
        self.assertEqual((job.email, job.nonce), ('', ''))
        self.assertEqual(len(mail.outbox), 1)
        self.assertFalse(deliver_one())

    def test_stale_queue_jobs_not_sent_and_cleanup_retains_current_buckets(self):
        queue_reset(self.user.email)
        AuthMailJob.objects.update(created_at=timezone.now()-timedelta(hours=2))
        deliver_one()
        self.assertEqual(len(mail.outbox), 0)
        AuthMailJob.objects.update(completed_at=timezone.now()-timedelta(days=8))
        consume_limits([('old', 1, 3600)])
        AuthRateBucket.objects.update(started_at=timezone.now()-timedelta(days=3))
        consume_limits([('current', 1, 3600)])
        cleanup()
        self.assertFalse(AuthMailJob.objects.exists())
        self.assertEqual(AuthRateBucket.objects.count(), 1)

    def test_trusted_tunnel_ip_is_explicit_and_ipv6_is_grouped(self):
        request = RequestFactory().get('/', REMOTE_ADDR='10.0.0.2', HTTP_CF_CONNECTING_IP='203.0.113.4')
        self.assertEqual(client_ip(request), '10.0.0.2')
        with override_settings(AUTH_TRUST_CLOUDFLARE_IP=True):
            self.assertEqual(client_ip(request), '203.0.113.4')
        request.META['REMOTE_ADDR'] = '2001:db8:abcd:12::5'
        self.assertEqual(client_ip(request), '2001:db8:abcd:12::/64')

    def test_logs_redact_tokens(self):
        record = logging.LogRecord('test', logging.WARNING, '', 0, 'Denied: %s',
            ('/account/email/confirm/secret-token/',), None)
        RedactAuthLinks().filter(record)
        self.assertNotIn('secret-token', record.getMessage())
