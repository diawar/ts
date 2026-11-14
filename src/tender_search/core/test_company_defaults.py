from importlib import import_module
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, override_settings

from .models import CompanySettings


MIGRATION = import_module('tender_search.core.migrations.0059_empty_company_defaults')
DETAIL_FIELDS = tuple(MIGRATION.DETAIL_LENGTHS)


@override_settings(STORAGES={
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
})
class CompanyDefaultsTests(TestCase):
    def assert_empty_details(self, company):
        self.assertTrue(all(getattr(company, name) == '' for name in DETAIL_FIELDS))

    def test_model_and_lazy_settings_start_with_empty_details(self):
        owner = get_user_model().objects.create_user('empty-settings')
        self.assert_empty_details(CompanySettings(owner=owner))
        self.client.force_login(owner)
        response = self.client.get('/settings/')
        self.assertEqual(response.status_code, 200)
        company = CompanySettings.objects.get(owner=owner)
        self.assert_empty_details(company)
        self.assertTrue(all(response.context['company_form'].initial[name] == '' for name in DETAIL_FIELDS))

    def test_admin_created_account_has_no_installation_details(self):
        admin = get_user_model().objects.create_superuser('defaults-admin')
        self.client.force_login(admin)
        response = self.client.post('/administration/', {
            'username': 'created-by-admin', 'email': 'created@example.test',
            'password1': 'Defaults-strong-password-8132!',
            'password2': 'Defaults-strong-password-8132!',
        })
        self.assertEqual(response.status_code, 302)
        owner = get_user_model().objects.get(username='created-by-admin')
        self.client.force_login(owner)
        self.assertEqual(self.client.get('/settings/').status_code, 200)
        self.assert_empty_details(CompanySettings.objects.get(owner=owner))

    def test_first_settings_visit_preserves_custom_details_without_bin(self):
        owner = get_user_model().objects.create_user('custom-settings')
        company = CompanySettings.objects.create(owner=owner, company_name='Custom company',
            legal_address='Custom address', bank_account='KZ000000000000000000')
        before = {name: getattr(company, name) for name in DETAIL_FIELDS}
        self.client.force_login(owner)
        self.assertEqual(self.client.get('/settings/').status_code, 200)
        company.refresh_from_db()
        self.assertEqual({name: getattr(company, name) for name in DETAIL_FIELDS}, before)


class CompanyDefaultsMigrationTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.before_apps = MigrationExecutor(connection).loader.project_state([
            ('core', '0058_specification_shared_revision'),
        ]).apps

    def create_legacy(self, username, **fields):
        owner = get_user_model().objects.create_user(username)
        Company = self.before_apps.get_model('core', 'CompanySettings')
        return Company.objects.create(owner_id=owner.pk, **fields)

    def clean(self):
        MIGRATION.clear_inherited_details(self.before_apps, SimpleNamespace(connection=connection))

    def test_cleanup_removes_only_inherited_details_from_blank_bin_account(self):
        target = self.create_legacy('inherited-details')
        legitimate = self.create_legacy('filled-bin', company_bin='111111111111')
        before = {name: getattr(legitimate, name) for name in DETAIL_FIELDS}
        self.clean()
        target.refresh_from_db(); legitimate.refresh_from_db()
        self.assertTrue(all(getattr(target, name) == '' for name in DETAIL_FIELDS))
        self.assertEqual({name: getattr(legitimate, name) for name in DETAIL_FIELDS}, before)
        self.clean()  # Safe to repeat without restoring any inherited values.
        target.refresh_from_db()
        self.assertTrue(all(getattr(target, name) == '' for name in DETAIL_FIELDS))

    def test_cleanup_preserves_custom_fields_phone_and_custom_bank_account(self):
        mixed = self.create_legacy('mixed-details', company_name='Own company',
            address_city='Own city', contact_phone='+77000000000')
        own_bank = self.create_legacy('own-bank', bank_account='KZ000000000000000000')
        before = {name: getattr(own_bank, name) for name in DETAIL_FIELDS}
        self.clean()
        mixed.refresh_from_db(); own_bank.refresh_from_db()
        self.assertEqual(mixed.company_name, 'Own company')
        self.assertEqual(mixed.address_city, 'Own city')
        self.assertEqual(mixed.contact_phone, '+77000000000')
        self.assertEqual(mixed.bank_account, '')
        self.assertEqual(mixed.legal_address, '')
        self.assertEqual({name: getattr(own_bank, name) for name in DETAIL_FIELDS}, before)
