from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings

from .models import CompanySettings


@override_settings(STORAGES={
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class UniqueCompanyBinTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user('bin-owner')
        self.other = get_user_model().objects.create_user('bin-other')
        self.company = CompanySettings.objects.create(
            owner=self.owner, company_bin='123456789012')
        self.client.force_login(self.other)

    def test_new_accounts_start_without_bin(self):
        self.assertEqual(self.client.get('/settings/').status_code, 200)
        self.assertEqual(CompanySettings.objects.get(owner=self.other).company_bin, '')

    def test_duplicate_is_rejected_and_own_bin_can_be_saved(self):
        response = self.client.post('/settings/', {
            'action': 'company', 'company_bin': self.company.company_bin})
        self.assertContains(response, 'Этот БИН уже используется другим аккаунтом.')
        self.assertEqual(CompanySettings.objects.get(owner=self.other).company_bin, '')
        self.client.force_login(self.owner)
        response = self.client.post('/settings/', {
            'action': 'company', 'company_bin': self.company.company_bin})
        self.assertRedirects(response, '/settings/')

    def test_database_prevents_duplicates_and_allows_empty_bins(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            CompanySettings.objects.create(owner=self.other, company_bin=self.company.company_bin)
        CompanySettings.objects.create(owner=self.other)
        third = get_user_model().objects.create_user('bin-third')
        CompanySettings.objects.create(owner=third)
