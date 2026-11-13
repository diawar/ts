from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from zipfile import ZipFile

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.db import connection
from django.test.utils import CaptureQueriesContext
from datetime import timedelta
from django.utils import timezone
from pypdf import PdfWriter
from tender_search.client import Document, SourceError
from .models import LotSpecification, Favorite, AnnouncementWatch
from .search_service import store_lot
from .specification_service import extract_text, process_specification, queue_specification
from .test_index import row


@override_settings(STORAGES={'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
                            'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class SpecificationTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user('spec-owner')
        self.other = get_user_model().objects.create_user('other-spec-owner')
        self.lot = store_lot(row(1))
        self.lot.refresh_state = 'done'; self.lot.checked_at = timezone.now(); self.lot.save()
        self.client.force_login(self.owner)

    def test_docx_rejects_dtd_and_entities_before_expansion_or_external_reads(self):
        with TemporaryDirectory() as directory:
            path=Path(directory)/'unsafe.docx'
            for entity in ('"unsafe"','SYSTEM "file:///tmp/never-read-this-file"'):
                with ZipFile(path,'w') as archive:
                    archive.writestr('word/document.xml',
                        '<!DOCTYPE doc [<!ENTITY value '+entity+'>]>'
                        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                        '<w:body><w:p><w:r><w:t>&value;</w:t></w:r></w:p></w:body></w:document>')
                with self.assertRaisesMessage(SourceError,'запрещённые XML'):
                    extract_text(path,'unsafe.docx')

    def test_saved_text_returns_in_post_without_queue_or_source_request(self):
        LotSpecification.objects.create(owner=self.owner,lot=self.lot,state='done',
            documents=[{'name':'Требования','text':'Сохранённый USB 3.0','source_url':'https://files.example/1','scope':'lot'}])
        with patch('tender_search.core.tasks.extract_lot_specification.delay') as task, \
                patch('tender_search.core.specification_service.lot_client') as source:
            response=self.client.post(f'/lots/{self.lot.pk}/specification/',HTTP_X_LOT_ASYNC='1')
        self.assertFalse(response.json()['queued'])
        self.assertIn('Сохранённый USB 3.0',response.json()['specification_html'])
        task.assert_not_called();source.assert_not_called()
        self.assertEqual(response['Cache-Control'],'private, no-store')

    def test_complete_shared_cache_is_reused_only_after_request_and_respects_scope(self):
        from .models import Document as SavedDocument, DocumentVersion, TechnicalDocumentIndex
        other_lot=store_lot(row(2))
        other_lot.announcement=self.lot.announcement;other_lot.save()
        for identifier,lot,text in [('a',self.lot,'Наш лот'),('b',None,'Общий документ'),('c',other_lot,'Чужой лот')]:
            doc=SavedDocument.objects.create(announcement=self.lot.announcement,lot=lot,source_id=identifier,
                name='Техспецификация',url='https://files.example/'+identifier,kind='technical',status='ready')
            version=DocumentVersion.objects.create(document=doc,sha256=identifier*64,text=text)
            doc.current_version=version;doc.save()
        TechnicalDocumentIndex.objects.create(lot=self.lot,state='ready',checked_at=timezone.now())
        self.assertNotContains(self.client.get(f'/lots/{self.lot.pk}/'),'Наш лот')
        with patch('tender_search.core.tasks.extract_lot_specification.delay') as task:
            response=self.client.post(f'/lots/{self.lot.pk}/specification/',HTTP_X_LOT_ASYNC='1')
        task.assert_not_called()
        self.assertFalse(response.json()['queued'])
        self.assertIn('Наш лот',response.json()['specification_html'])
        self.assertIn('Общий документ',response.json()['specification_html'])
        self.assertNotIn('Чужой лот',response.json()['specification_html'])

    def test_partial_cache_does_not_claim_completion_and_refresh_preserves_old_text(self):
        job=LotSpecification.objects.create(owner=self.owner,lot=self.lot,state='partial',documents=[{'name':'Файл','text':'Старый текст'}])
        with patch('tender_search.core.tasks.extract_lot_specification.delay') as task:
            response=self.client.post(f'/lots/{self.lot.pk}/specification/',{'refresh':'1'},HTTP_X_LOT_ASYNC='1')
        task.assert_called_once()
        self.assertTrue(response.json()['queued'])
        self.assertIn('Старый текст',response.json()['specification_html'])
        job.refresh_from_db();self.assertEqual(job.state,'queued')

    def test_done_text_can_be_explicitly_refreshed_and_route_is_dedicated(self):
        from tender_search.web.celery import app
        job=LotSpecification.objects.create(owner=self.owner,lot=self.lot,state='done',documents=[{'text':'USB'}])
        with patch('tender_search.core.tasks.extract_lot_specification.delay') as task:
            queue_specification(self.owner,self.lot,refresh=True)
        task.assert_called_once()
        job.refresh_from_db();self.assertEqual(job.documents,[{'text':'USB'}])
        route=app.amqp.router.route({},'tender_search.core.tasks.extract_lot_specification')
        self.assertEqual(route['queue'].name,'specifications')

    def test_extract_formats_and_explicit_scan_failure(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'spec'
            path.write_text('Мышь беспроводная\nКоличество: 2', encoding='utf-8')
            self.assertIn('Количество: 2', extract_text(path, 'spec.txt')[0])
            with ZipFile(path, 'w') as archive:
                archive.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Клавиатура USB</w:t></w:r></w:p></w:body></w:document>')
            self.assertEqual(extract_text(path, 'spec.docx'), ('Клавиатура USB', False))
            writer = PdfWriter(); writer.add_blank_page(width=100, height=100)
            with path.open('wb') as output: writer.write(output)
            with self.assertRaisesMessage(SourceError, 'OCR'): extract_text(path, 'scan.pdf')
            path.write_bytes(b'unsupported')
            with self.assertRaises(SourceError): extract_text(path, 'spec.doc')

    def test_document_scope_private_text_and_no_star_or_plane_side_effects(self):
        files = [Document(str(i), name, f'https://files.example/{i}', self.lot.announcement.source_id, lot_id, 'Техническая спецификация' if i != 4 else 'Договор')
                 for i, name, lot_id in [(1, 'Техспецификация.txt', self.lot.source_id), (2, 'Техспецификация.txt', None),
                                         (3, 'Техспецификация.txt', self.lot.source_id + 1), (4, 'Договор.txt', self.lot.source_id)]]
        job = LotSpecification.objects.create(owner=self.owner, lot=self.lot, state='queued', generation='job')
        with TemporaryDirectory() as directory, override_settings(MEDIA_ROOT=directory), patch('tender_search.core.specification_service.lot_client') as factory:
            api = factory.return_value.__enter__.return_value
            api.documents.return_value = files
            def download(file, destination, **kwargs):
                path = destination / (file.id + '.txt')
                path.write_text('<script>Характеристики ' + file.id + '</script>', encoding='utf-8')
                return {'path': str(path), 'sha256': file.id.zfill(64)}
            api.download.side_effect = download
            process_specification(job.pk, 'stale')
            api.documents.assert_not_called()
            process_specification(job.pk, 'job')
            self.assertEqual(api.download.call_count, 2)
            self.assertFalse(list((Path(directory) / 'specification-work').iterdir()))
        job.refresh_from_db()
        self.assertEqual(job.state, 'done')
        self.assertEqual([doc['scope'] for doc in job.documents], ['lot', 'announcement'])
        response = self.client.get(f'/lots/{self.lot.pk}/')
        self.assertContains(response, '&lt;script&gt;Характеристики 1&lt;/script&gt;')
        self.assertNotContains(response, '<script>Характеристики')
        self.client.force_login(self.other)
        self.assertNotContains(self.client.get(f'/lots/{self.lot.pk}/'), 'Характеристики 1')
        self.assertFalse(Favorite.objects.exists()); self.assertFalse(AnnouncementWatch.objects.exists())

    def test_enqueue_requires_login_and_does_not_duplicate_pending_job(self):
        endpoint = f'/lots/{self.lot.pk}/specification/'
        with patch('tender_search.core.tasks.extract_lot_specification.delay') as queued:
            self.assertEqual(self.client.get(endpoint).status_code, 405)
            for _ in range(2): self.assertEqual(self.client.post(endpoint).status_code, 302)
            queued.assert_called_once()
            self.assertEqual(LotSpecification.objects.filter(owner=self.owner).count(), 1)
            self.client.logout()
            self.assertEqual(self.client.post(endpoint).status_code, 302)
            queued.assert_called_once()

    def test_async_extract_stays_on_card_and_poll_only_sends_changed_content(self):
        with patch('tender_search.core.tasks.extract_lot_specification.delay'):
            response = self.client.post(f'/lots/{self.lot.pk}/specification/', HTTP_X_LOT_ASYNC='1')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['queued'])
        endpoint = f'/lots/{self.lot.pk}/status/'
        first = self.client.get(endpoint).json()
        self.assertTrue(first['pending'])
        self.assertIn('Извлекаем', first['html'])
        self.assertNotIn('hx-trigger="load delay:2s"', first['html'])
        with CaptureQueriesContext(connection) as queries:
            unchanged = self.client.get(endpoint, first['revisions'])
        self.assertNotIn('html', unchanged.json())
        self.assertLess(len(unchanged.content), 300)
        self.assertFalse(any('"core_lotspecification"."documents"' in query['sql'] for query in queries))
        self.assertEqual(unchanged['Cache-Control'], 'private, no-store')
        LotSpecification.objects.filter(owner=self.owner).update(state='done', documents=[{'name':'Требования', 'text':'USB 3.0'}], updated_at=timezone.now())
        completed = self.client.get(endpoint, first['revisions']).json()
        self.assertFalse(completed['pending'])
        self.assertIn('USB 3.0', completed['html'])
        self.assertNotIn('html', self.client.get(endpoint, completed['revisions']).json())

    def test_poll_is_private_does_not_refresh_source_or_write_navigation(self):
        LotSpecification.objects.create(owner=self.other, lot=self.lot, state='done', documents=[{'text':'Private other user'}])
        self.lot.checked_at = timezone.now()-timedelta(hours=1)
        self.lot.save()
        with patch('tender_search.core.views.queue_refresh') as refresh:
            response = self.client.get(f'/lots/{self.lot.pk}/status/')
        refresh.assert_not_called()
        self.assertNotIn('Private other user', response.json()['html'])
        self.assertNotIn(f'lot_return_{self.lot.pk}', self.client.session)
        session = self.client.session
        session[f'lot_return_{self.lot.pk}'] = '/favorites/'
        session.save()
        response = self.client.get(f'/lots/{self.lot.pk}/status/', HTTP_REFERER=f'http://testserver/lots/{self.lot.pk}/')
        self.assertIn('name="next" value="/favorites/"', response.json()['html'])
        self.client.logout()
        self.assertEqual(self.client.get(f'/lots/{self.lot.pk}/status/').status_code, 302)

    def test_stalled_extraction_stops_polling_and_offers_retry(self):
        job = LotSpecification.objects.create(owner=self.owner, lot=self.lot, state='running')
        LotSpecification.objects.filter(pk=job.pk).update(updated_at=timezone.now()-timedelta(minutes=16))
        response = self.client.get(f'/lots/{self.lot.pk}/status/').json()
        self.assertFalse(response['pending'])
        self.assertIn('Извлечение не завершилось вовремя', response['html'])
        job.refresh_from_db()
        self.assertEqual(job.state, 'error')

    def test_description_displays_repeated_source_lines_once_and_escapes_markup(self):
        self.lot.description = 'Особое описание\n<script>unsafe</script>\nОсобое описание'
        self.lot.save()
        response = self.client.get(f'/lots/{self.lot.pk}/')
        self.assertContains(response, 'Особое описание', count=1)
        self.assertContains(response, '&lt;script&gt;unsafe&lt;/script&gt;')
        self.assertNotContains(response, '<script>unsafe</script>')
        self.assertContains(response, 'class="lot-conditions"')
        self.lot.refresh_from_db()
        self.assertEqual(self.lot.description.count('Особое описание'), 2)
