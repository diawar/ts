from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.management import call_command, CommandError
from django.test import TestCase, override_settings

from .lead_service import collect_lead_round, set_collection_enabled
from .models import LeadCollectionState


@override_settings(LEAD_COLLECTION_AVAILABLE=False, STORAGES={
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class GoszakupkiEditionTests(TestCase):
    def test_staff_cannot_access_or_enable_client_collection(self):
        user = get_user_model().objects.create_user('edition-admin', is_staff=True)
        self.client.force_login(user)
        for path in ('/administration/clients/', '/administration/collection-status/',
                     '/administration/clients/export/', '/administration/clients/1/'):
            self.assertEqual(self.client.get(path).status_code, 404)
        self.assertEqual(self.client.post('/administration/clients/', {'action': 'start'}).status_code, 404)
        self.assertFalse(LeadCollectionState.objects.exists())
        page = self.client.get('/main/')
        self.assertContains(page, 'Tender<span>Search</span>')
        self.assertNotContains(page, 'Администрирование')
        self.assertNotContains(page, 'Гос<span>закупки</span>')

    def test_collector_is_inert_even_when_old_database_state_is_enabled(self):
        LeadCollectionState.objects.create(enabled=True)
        api = Mock()
        self.assertFalse(collect_lead_round(api))
        self.assertEqual(api.mock_calls, [])
        with self.assertRaises(ValueError):
            set_collection_enabled(True)
        with patch('tender_search.core.management.commands.collect_leads.source_client') as source:
            with self.assertRaises(CommandError):
                call_command('collect_leads', enable=True)
            source.assert_not_called()

    def test_login_brand(self):
        self.assertContains(self.client.get('/login/'), 'Вход — TenderSearch')
