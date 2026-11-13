from types import SimpleNamespace
from django.test import SimpleTestCase, TestCase
from django.template.loader import render_to_string
from .models import CompanySettings, Lot


class LotControlTests(SimpleTestCase):
    def test_company_defaults_exclude_personal_phone(self):
        company = CompanySettings()
        self.assertEqual(company.company_bin, '')
        self.assertEqual(company.address_office, '25Б')
        self.assertEqual(company.bank_account, 'KZ92601A241008980281')
        self.assertEqual(company.contact_phone, '')
        self.assertTrue(company.is_vat_payer)

    def test_portal_link_opens_cabinet_announcement_and_number_is_escaped(self):
        lot = Lot(source_id=123, number='123-ЗЦП1')
        from .models import Announcement
        lot.announcement = Announcement(source_id=999, external_id=17929324, source='zakup')
        self.assertEqual(lot.announcement_portal_url,
                         'https://zakup.gov.kz/cabinet/announcement/17929324')
        lot.announcement.external_id = None
        lot.announcement.number = '17929324-1'
        self.assertEqual(lot.announcement_portal_url,
                         'https://zakup.gov.kz/cabinet/announcement/17929324')
        lot.announcement.number = ''
        self.assertEqual(lot.announcement_portal_url, '')
        html = render_to_string('core/_copy_lot_number.html', {'lot': SimpleNamespace(number='123"<')})
        self.assertIn('data-copy-text="123&quot;&lt;"', html)
        self.assertIn('type="button"', html)


class CompanyDefaultsMigrationTests(TestCase):
    def test_fill_existing_is_idempotent_and_preserves_personal_values(self):
        import importlib
        from django.apps import apps
        from django.contrib.auth import get_user_model
        from django.db import connection
        migration = importlib.import_module('tender_search.core.migrations.0045_company_defaults')
        owner = get_user_model().objects.create_user('existing-company')
        company = CompanySettings.objects.create(owner=owner, company_bin='', company_name='',
            bank_bic='', contact_phone='+77000000000', is_vat_payer=False)
        other = get_user_model().objects.create_user('other-company')
        CompanySettings.objects.create(owner=other, company_bin='123456789012', bank_bic='')
        schema_editor = SimpleNamespace(connection=connection)
        migration.fill_empty(apps, schema_editor)
        migration.fill_empty(apps, schema_editor)
        company.refresh_from_db()
        self.assertEqual(company.company_bin, '241040001415')
        self.assertEqual(company.bank_bic, 'HSBKKZKX')
        self.assertEqual(company.contact_phone, '+77000000000')
        self.assertFalse(company.is_vat_payer)
        self.assertEqual(CompanySettings.objects.get(owner=other).bank_bic, '')
