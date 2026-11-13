from datetime import timedelta
import csv
from io import StringIO
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, SimpleTestCase, override_settings
from django.utils import timezone

from tender_search.client import Page, SourceError
from .lead_source import Participant, ProtocolFormatError, parse_protocol_text, parse_protocol_html, parse_registry_card, LeadPortal
from .lead_service import claim_collection, collect_contact, collect_protocol, discover_page, collect_lead_round
from .models import LeadCollectionState, LeadCompany, LeadEmail, LeadParticipation, LeadProcurement


PROTOCOL = '''Заказчик 111111111111, Школа
Протокол об итогах 123-ЗЦП1
Лот №                       100-ЗЦП1
№   Наименование поставщика       БИН (ИИН)              Цена
1   ТОО Alpha                     123456789012            1000
    Engineering
2   ИП Beta                       987654321012            2000

1. Определить победителем по лоту № 100-ЗЦП1 : 123456789012 ТОО Alpha Engineering
Лот №                       101-ЗЦП1
№   Наименование поставщика       БИН (ИИН)              Цена
1   ТОО Alpha Engineering         123456789012            1000
'''


def registry_html(identifier='123456789012', email='sales@example.com; second@example.org'):
    return f'''<table><tr><td>БИН участника</td><td>{identifier}</td></tr>
      <tr><td>Наименование на рус. языке</td><td>ТОО Alpha Engineering</td></tr>
      <tr><td>E-Mail:</td><td>{email}</td></tr></table><footer>support@portal.example</footer>'''


class LeadParserTests(SimpleTestCase):
    def test_supplier_rows_and_wrapped_names_exclude_customer(self):
        people, unresolved = parse_protocol_text(PROTOCOL)
        self.assertEqual(unresolved, 0)
        self.assertEqual({p.identifier for p in people}, {'123456789012', '987654321012'})
        first = next(p for p in people if p.identifier == '123456789012')
        self.assertEqual(first.name, 'ТОО Alpha Engineering')
        self.assertEqual(first.lots, {'100-ЗЦП1', '101-ЗЦП1'})
        self.assertTrue(first.winner)

    def test_reverse_columns_and_foreign_identifiers_need_review(self):
        people, unresolved = parse_protocol_text('''Протокол об итогах
№ БИН (ИИН)   Наименование поставщика       Адрес
1 123456789012 ТОО Alpha                   Казахстан
2 Foreign LLC 1234567890                   Another country
''')
        self.assertEqual([p.identifier for p in people], ['123456789012'])
        self.assertEqual(people[0].name, 'ТОО Alpha')
        self.assertEqual(unresolved, 1)

    def test_other_tables_are_not_supplier_contacts(self):
        people, _ = parse_protocol_text(PROTOCOL+'''Члены комиссии
№ ФИО                             ИИН
1   Иванов                        222222222222
''')
        self.assertNotIn('222222222222', {p.identifier for p in people})

    def test_single_source_supplier_table_without_row_numbers(self):
        people, unresolved = parse_protocol_text('''Протокол об итогах
№ лота: 123-ОИ2
Информация о приглашенном потенциальном поставщике:
        Наименование поставщика               БИН (ИИН)             Реквизиты
 Товарищество "Alpha"                         123456789012          Адрес: Казахстан
Информация о ценовом предложении потенциального поставщика:
      Выделенная сумма               Цена поставщика
 1000                                1000
Решено заключить договор с приглашенным поставщиком.
''')
        self.assertEqual(unresolved, 0)
        self.assertEqual([p.identifier for p in people], ['123456789012'])
        self.assertEqual(people[0].lots, {'123-ОИ2'})
        self.assertTrue(people[0].winner)

    def test_unknown_format_is_not_successful_empty_collection(self):
        with self.assertRaises(ProtocolFormatError):
            parse_protocol_text('Заказчик 111111111111. Победитель 123456789012.')
        people, unresolved = parse_protocol_text('''Протокол об итогах
Представлены следующие ценовые предложения: 0
№ Наименование поставщика       БИН (ИИН)
''')
        self.assertEqual((people, unresolved), ([], 0))

    def test_contact_identity_and_email_field_only(self):
        result = parse_registry_card(registry_html(), '123456789012')
        self.assertEqual(result['emails'], ['sales@example.com', 'second@example.org'])
        self.assertEqual(result['kind'], 'bin')
        with self.assertRaises(SourceError):
            parse_registry_card(registry_html('999999999999'), '123456789012')
        self.assertEqual(parse_registry_card(registry_html(email='не указан'), '123456789012')['emails'], [])

    def test_html_protocol_deduplicates_suppliers_and_handles_no_bids(self):
        html = '''<html><table><tr><th>Заказчик</th><th>БИН</th></tr><tr><td>Школа</td><td>111111111111</td></tr></table>
        <table><tr><th>№</th><th>БИН (ИИН)</th><th>Наименование поставщика</th></tr>
        <tr><td>1</td><td>123456789012</td><td>Alpha</td></tr><tr><td>2</td><td>123456789012</td><td>Alpha</td></tr></table></html>'''
        people, unresolved = parse_protocol_html(html)
        self.assertEqual(([p.identifier for p in people], unresolved), (['123456789012'], 0))
        self.assertEqual(parse_protocol_html('<html>Отсутствие представленных заявок</html>'), ([], 0))
        self.assertEqual(parse_protocol_html('''<html><table><caption>Информация о представленных заявках на участие в конкурсе (лоте): 0</caption>
          <tr><th>Наименование потенциального поставщика</th><th>БИН (ИИН)</th></tr><tr><td></td><td></td></tr></table></html>'''), ([], 0))
        with self.assertRaises(ProtocolFormatError):
            parse_protocol_html('<html>Войти. support@example.com</html>')

    def test_protocol_url_cannot_point_outside_public_source(self):
        portal = LeadPortal(Mock())
        for url in ['https://example.org/files/download_file/1/2/', 'http://v3bl.goszakup.gov.kz/files/download_file/1/2/',
                    'https://old.goszakup.gov.kz/ru/admin/1', 'https://user:secret@old.goszakup.gov.kz/ru/registry/show_supplier/1']:
            with self.assertRaises(SourceError):
                portal.read(url)

    def test_supplier_business_role_is_preferred_to_person_with_same_iin(self):
        api = Mock()
        api.get_json.return_value = {'results': [
            {'iin_bin': '123456789012', 'external_id': 10, 'is_supplier': True},
            {'iin_bin': '123456789012', 'external_id': 20, 'is_supplier': False}]}
        portal = LeadPortal(api)
        portal.read = Mock(return_value=registry_html().encode())
        self.assertEqual(portal.contacts('123456789012')['registry_id'], 10)
        portal.read.assert_called_once_with('https://old.goszakup.gov.kz/ru/registry/show_supplier/10')
        api.get_json.return_value['results'][1]['is_supplier'] = True
        with self.assertRaises(SourceError):
            portal.contacts('123456789012')


@override_settings(LEAD_COLLECTION_AVAILABLE=True)
class LeadCollectionTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.state = LeadCollectionState.objects.create(enabled=True, windows=[{
            'lo': (self.now-timedelta(days=1)).isoformat(), 'hi': self.now.isoformat(), 'offset': 0}])

    def procurement(self, source_id=1, **kwargs):
        return LeadProcurement.objects.create(source_id=source_id, external_id=100+source_id,
            number=f'{source_id}-1', name='Закупка', results_published_at=self.now, **kwargs)

    def test_discovery_is_replay_safe_and_checks_source_filters(self):
        row = {'id': 10, 'external_id': 20, 'announcement_number': '20-1', 'name': 'Товары',
               'system_id': 1, 'results_publication_date': (self.now-timedelta(hours=1)).isoformat()}
        api = Mock()
        api.search.return_value = Page(1, [row], None)
        discover_page(api, self.state)
        self.assertEqual(LeadProcurement.objects.count(), 1)
        self.state.windows = [{'lo': (self.now-timedelta(days=1)).isoformat(), 'hi': self.now.isoformat(), 'offset': 0}]
        discover_page(api, self.state)
        self.assertEqual(LeadProcurement.objects.count(), 1)
        self.state.windows = [{'lo': (self.now-timedelta(days=1)).isoformat(), 'hi': self.now.isoformat(), 'offset': 0}]
        api.search.return_value = Page(1, [dict(row, system_id=3)], None)
        with self.assertRaises(SourceError):
            discover_page(api, self.state)
        self.assertEqual(self.state.windows[0]['offset'], 0)

    def test_large_result_window_is_split_without_losing_dates(self):
        api = Mock()
        api.search.return_value = Page(10000, [], 'next')
        lo, hi = self.state.windows[0]['lo'], self.state.windows[0]['hi']
        discover_page(api, self.state)
        self.assertEqual(len(self.state.windows), 2)
        self.assertEqual(self.state.windows[0]['hi'], hi)
        self.assertEqual(self.state.windows[1]['lo'], lo)
        self.assertEqual(self.state.windows[0]['lo'], self.state.windows[1]['hi'])

    def test_every_participant_is_saved_once_with_provenance(self):
        source = self.procurement()
        portal = Mock()
        people, _ = parse_protocol_text(PROTOCOL)
        portal.protocol.return_value = (people, 0, 'https://v3bl.goszakup.gov.kz/files/download_file/1/2/')
        collect_protocol(portal)
        source.refresh_from_db()
        self.assertEqual(source.state, 'done')
        self.assertEqual((LeadCompany.objects.count(), LeadParticipation.objects.count()), (2, 2))
        source.state = 'queued'
        source.save()
        collect_protocol(portal)
        self.assertEqual((LeadCompany.objects.count(), LeadParticipation.objects.count()), (2, 2))
        self.assertTrue(LeadParticipation.objects.get(company__identifier='123456789012').winner)

    def test_failed_protocol_does_not_block_next_procurement(self):
        one, two = self.procurement(1), self.procurement(2)
        portal = Mock()
        portal.protocol.side_effect = [SourceError('HTTP 404'), ([Participant('123456789012', 'Alpha')], 0,
            'https://v3bl.goszakup.gov.kz/files/download_file/1/2/')]
        collect_protocol(portal)
        collect_protocol(portal)
        one.refresh_from_db(); two.refresh_from_db()
        self.assertEqual({one.state, two.state}, {'retry', 'done'})
        self.assertEqual(LeadCompany.objects.count(), 1)

    def test_contacts_keep_old_addresses_on_error_then_reverify(self):
        company = LeadCompany.objects.create(identifier='123456789012', name='Alpha')
        old = LeadEmail.objects.create(company=company, email='old@example.com', source_url='https://example.com')
        portal = Mock()
        portal.contacts.side_effect = SourceError('Карточка не подтвердила БИН')
        collect_contact(portal)
        old.refresh_from_db(); company.refresh_from_db()
        self.assertTrue(old.current)
        self.assertEqual(company.contact_state, 'error')
        LeadCompany.objects.filter(pk=company.pk).update(next_contact_check_at=self.now)
        portal.contacts.side_effect = None
        portal.contacts.return_value = {'name': 'Alpha official', 'kind': 'bin', 'registry_id': 10,
            'source_url': 'https://old.goszakup.gov.kz/ru/registry/show_supplier/10', 'emails': ['new@example.com']}
        collect_contact(portal)
        old.refresh_from_db(); company.refresh_from_db()
        self.assertFalse(old.current)
        self.assertEqual(company.contact_state, 'ready')
        self.assertEqual(list(company.emails.filter(current=True).values_list('email', flat=True)), ['new@example.com'])
        LeadCompany.objects.filter(pk=company.pk).update(next_contact_check_at=self.now)
        portal.contacts.return_value['emails'] = []
        collect_contact(portal)
        company.refresh_from_db()
        self.assertEqual(company.contact_state, 'missing')
        self.assertFalse(company.emails.filter(current=True).exists())

    def test_pause_lease_and_exclusions_are_respected(self):
        self.state.enabled = False; self.state.save()
        self.assertIsNone(claim_collection())
        self.state.enabled = True; self.state.lease_until = self.now+timedelta(minutes=1); self.state.save()
        self.assertIsNone(claim_collection())
        company = LeadCompany.objects.create(identifier='123456789012', name='Alpha', status='excluded')
        portal = Mock()
        self.assertFalse(collect_contact(portal))
        portal.contacts.assert_not_called()

    def test_discovery_failure_still_processes_existing_queue(self):
        self.procurement()
        api = Mock()
        api.search.side_effect = SourceError('Источник временно недоступен')
        with patch('tender_search.core.lead_service.LeadPortal') as factory:
            factory.return_value.protocol.return_value = ([Participant('123456789012', 'Alpha')], 0,
                'https://v3bl.goszakup.gov.kz/files/download_file/1/2/')
            factory.return_value.contacts.return_value = {'name': 'Alpha', 'kind': 'bin', 'registry_id': 10,
                'source_url': 'https://old.goszakup.gov.kz/ru/registry/show_supplier/10', 'emails': ['a@example.com']}
            collect_lead_round(api)
        self.assertEqual(LeadEmail.objects.count(), 1)
        self.state.refresh_from_db()
        self.assertIn('недоступен', self.state.error)


@override_settings(STORAGES={'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
                            'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}})
@override_settings(LEAD_COLLECTION_AVAILABLE=True)
class LeadAdministrationTests(TestCase):
    def setUp(self):
        self.staff = get_user_model().objects.create_user('lead-admin', is_staff=True,is_superuser=True)
        self.user = get_user_model().objects.create_user('lead-user')
        self.company = LeadCompany.objects.create(identifier='123456789012', name='Alpha')
        self.url = '/administration/clients/'

    def test_only_staff_can_read_or_manage_leads(self):
        self.assertEqual(self.client.get(self.url).status_code, 302)
        self.client.force_login(self.user)
        for url in [self.url, '/administration/collection-status/', '/administration/clients/export/',
                    f'/administration/clients/{self.company.pk}/']:
            self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(self.url, {'action': 'start'}).status_code, 403)
        self.assertFalse(LeadCollectionState.objects.exists())

    def test_legacy_staff_without_admin_role_cannot_access_or_export_client_base(self):
        self.user.is_staff=True;self.user.save()
        self.client.force_login(self.user)
        for url in (self.url,'/administration/collection-status/','/administration/clients/export/',
                    f'/administration/clients/{self.company.pk}/'):
            self.assertEqual(self.client.get(url).status_code,403)
        self.assertEqual(self.client.post(self.url,{'action':'start'}).status_code,403)
        self.assertFalse(LeadCollectionState.objects.exists())

    def test_staff_page_and_controls_do_not_make_source_requests(self):
        self.client.force_login(self.staff)
        with patch('tender_search.core.source.source_client', side_effect=AssertionError('No HTTP from UI')):
            self.assertContains(self.client.get(self.url), 'База e-mail компаний')
            self.client.post(self.url, {'action': 'start'})
            self.assertTrue(LeadCollectionState.objects.get(pk=1).enabled)
            self.client.get(self.url)
            self.assertTrue(LeadCollectionState.objects.get(pk=1).enabled)
            self.client.post(self.url, {'action': 'pause'})
            self.assertFalse(LeadCollectionState.objects.get(pk=1).enabled)
        strict = Client(enforce_csrf_checks=True); strict.force_login(self.staff)
        self.assertEqual(strict.post(self.url, {'action': 'start'}).status_code, 403)

    def test_filter_detail_notes_and_csv_exclusion(self):
        LeadEmail.objects.create(company=self.company, email='sales@example.com', source_url='https://example.com')
        other = LeadCompany.objects.create(identifier='999999999999', name='Excluded', status='excluded')
        LeadEmail.objects.create(company=other, email='excluded@example.com', source_url='https://example.com')
        LeadEmail.objects.create(company=self.company, email='historical@example.com', current=False, source_url='https://example.com')
        self.client.force_login(self.staff)
        response = self.client.get(self.url, {'q': 'sales@example.com'})
        self.assertEqual(list(response.context['page']), [self.company])
        self.assertEqual(self.client.post(f'/administration/clients/{self.company.pk}/',
            {'status': 'contacted', 'notes': 'Обсудили демонстрацию'}).status_code, 302)
        self.company.refresh_from_db()
        self.assertEqual(self.company.status, 'contacted')
        self.assertContains(self.client.get(f'/administration/clients/{self.company.pk}/'), 'Обсудили демонстрацию')
        self.company.name = '=HYPERLINK("bad")'; self.company.save()
        response = self.client.get('/administration/clients/export/')
        body = b''.join(response.streaming_content).decode('utf-8-sig')
        self.assertIn('sales@example.com', body)
        self.assertIn("'=HYPERLINK", body)
        self.assertNotIn('excluded@example.com', body)
        self.assertNotIn('historical@example.com', body)

    def test_default_list_focuses_on_email_database_and_hides_protocol_diagnostics(self):
        LeadEmail.objects.create(company=self.company, email='sales@example.com', source_url='https://example.com')
        pending = LeadCompany.objects.create(identifier='111111111111', name='No email')
        excluded = LeadCompany.objects.create(identifier='222222222222', name='Do not contact', status='excluded')
        LeadEmail.objects.create(company=excluded, email='blocked@example.com', source_url='https://example.com')
        LeadProcurement.objects.create(source_id=99, external_id=99, number='99-1', name='Source',
            results_published_at=timezone.now(), state='review', error='Internal protocol diagnosis')
        self.client.force_login(self.staff)
        response = self.client.get(self.url)
        self.assertEqual(list(response.context['page']), [self.company])
        self.assertNotContains(response, 'Все компании')
        self.assertNotContains(response, 'компаний в базе')
        self.assertNotContains(response, 'Протоколы, требующие внимания')
        self.assertNotContains(response, 'Internal protocol diagnosis')
        self.assertNotContains(response, 'протоколов обработано')
        response = self.client.get(self.url, {'contact': ''})
        self.assertEqual(list(response.context['page']), [self.company])
        for contact in ['pending', 'missing', 'error']:
            response = self.client.get(self.url, {'contact': contact})
            self.assertEqual(list(response.context['page']), [self.company])
        self.assertEqual(response.context['lead_totals']['with_email'], 1)
        response = self.client.get(self.url, {'status': 'excluded'})
        self.assertEqual(list(response.context['page']), [excluded])

    def test_csv_has_company_email_columns_and_no_repeated_addresses(self):
        LeadEmail.objects.create(company=self.company, email='sales@example.com', source_url='https://example.com')
        alias = LeadCompany.objects.create(identifier='333333333333', name='Company alias')
        LeadEmail.objects.create(company=alias, email='SALES@example.com', source_url='https://example.com')
        self.client.force_login(self.staff)
        response = self.client.get('/administration/clients/export/')
        rows = list(csv.reader(StringIO(b''.join(response.streaming_content).decode('utf-8-sig')), delimiter=';'))
        self.assertEqual(rows[0], ['Компания', 'E-mail'])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1][1].casefold(), 'sales@example.com')
