from datetime import timedelta
from unittest.mock import patch
from django.test import TestCase
from django.contrib.auth import get_user_model
from django.utils import timezone
from .document_service import documents_needed
from .shipping import ShipmentForm
from .models import UserLotWorkflow, CompanySettings
from .lot_pricing import company_vat_rate
from .search_service import store_lot

class SharedDocumentPolicyTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('retention-owner')
        self.lot = store_lot({'id':900,'announcement_id':1900,'name_ru':'Test','customers':[]})

    def test_any_users_active_delivery_protects_shared_documents(self):
        deadline = timezone.now()-timedelta(days=120)
        self.assertFalse(documents_needed([self.lot.pk],deadline,'technical'))
        UserLotWorkflow.objects.create(owner=self.user,lot=self.lot,stage='delivery')
        self.assertTrue(documents_needed([self.lot.pk],deadline,'technical'))
        self.assertTrue(documents_needed([self.lot.pk],deadline,'act'))

    def test_archived_documents_expire_after_retention(self):
        old=timezone.now()-timedelta(days=100)
        UserLotWorkflow.objects.create(owner=self.user,lot=self.lot,stage='archived',archived_at=old)
        self.assertFalse(documents_needed([self.lot.pk],old,'act'))

    def test_delivery_requires_courier_details_and_clears_them_for_own_delivery(self):
        self.assertFalse(ShipmentForm({'mode':'courier'}).is_valid())
        form=ShipmentForm({'mode':'own','courier':'old','tracking_number':'old'})
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data['tracking_number'],'')

    def test_non_vat_payer_rate_is_zero(self):
        CompanySettings.objects.create(owner=self.user,is_vat_payer=False)
        self.assertEqual(company_vat_rate(self.user),0)

    def test_search_uses_latest_technical_version_only(self):
        from .models import Document, DocumentVersion
        from .technical_documents import matching_lots
        from tender_search.matching import PreparedMatcher
        self.lot.announcement.deadline=timezone.now()+timedelta(days=1)
        self.lot.announcement.save()
        doc=Document.objects.create(announcement=self.lot.announcement,lot=self.lot,source_id='tech',kind='technical',name='spec.txt',url='https://example.test/spec')
        DocumentVersion.objects.create(document=doc,sha256='a'*64,text='Мышь беспроводная')
        matcher=PreparedMatcher({'terms':['мыш'],'exclude':[]})
        self.assertTrue(matching_lots(matcher))
        DocumentVersion.objects.create(document=doc,sha256='b'*64,text='Клавиатура')
        self.assertFalse(matching_lots(matcher))

    def test_returning_to_known_content_updates_search_and_cache_version(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from .models import Document, DocumentVersion
        from .technical_documents import cache_file, matching_lots
        from tender_search.matching import PreparedMatcher
        self.lot.announcement.deadline = timezone.now() + timedelta(days=1)
        self.lot.announcement.save()
        api = Mock()
        file = SimpleNamespace(id='tech-reverted', lot_id=self.lot.source_id,
            name='spec.txt', url='https://example.test/a')
        with patch('tender_search.core.specification_service.extract_text') as extract:
            versions = []
            for url, digest, text in [('a', 'a'*64, 'Клавиатура'),
                                      ('b', 'b'*64, 'Мышь'),
                                      ('c', 'a'*64, 'Клавиатура')]:
                file.url = 'https://example.test/' + url
                api.download.return_value = {'sha256': digest, 'path': '/unused/spec.txt'}
                extract.return_value = (text, False)
                versions.append(cache_file(self.lot, file, api))
        doc = Document.objects.get(source_id=file.id)
        self.assertEqual(versions[0].pk, versions[2].pk)
        self.assertEqual(doc.current_version_id, versions[0].pk)
        self.assertEqual(DocumentVersion.objects.filter(document=doc).count(), 2)
        self.assertTrue(matching_lots(PreparedMatcher({'terms': ['клавиатур']})))
        self.assertFalse(matching_lots(PreparedMatcher({'terms': ['мыш']})))
        # Re-reading the unchanged URL must retain A even though B has a higher ID.
        api.reset_mock()
        with patch('tender_search.core.technical_documents.stored_path') as path:
            path.return_value.is_file.return_value = True
            self.assertEqual(cache_file(self.lot, file, api).pk, versions[0].pk)
        api.download.assert_not_called()


    def test_first_population_cannot_complete_with_error(self):
        from django.core.cache import cache
        from .models import TechnicalDocumentIndex,TechnicalIndexProgress
        from .technical_documents import update_progress
        from .test_index import put,row
        put(row(900,announcement_id=1900))
        idx=TechnicalDocumentIndex.objects.create(lot=self.lot,state='error')
        cache.delete('technical-progress-minute')
        update_progress()
        progress=TechnicalIndexProgress.objects.get()
        self.assertIsNone(progress.completed_at)
        self.assertEqual(progress.counts['errors'],1)
        idx.state='not_found';idx.save()
        cache.delete('technical-progress-minute')
        update_progress()
        progress.refresh_from_db()
        self.assertIsNotNone(progress.completed_at)
        self.assertEqual(progress.counts['missing'],1)


    def test_company_address_parts_compose_and_require_house(self):
        from .forms import CompanySettingsForm
        data={'is_vat_payer':'True','address_city':'Павлодар','address_street':'Желтоксан','address_house':'7','address_office':'25Б'}
        form=CompanySettingsForm(data)
        self.assertTrue(form.is_valid(),form.errors)
        self.assertEqual(form.cleaned_data['legal_address'],'Павлодар, Желтоксан, 7, 25Б')
        data['address_house']=''
        self.assertFalse(CompanySettingsForm(data).is_valid())
