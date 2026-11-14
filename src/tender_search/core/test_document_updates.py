from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from .document_service import cleanup_expired_documents, documents_needed, private_act_needed
from .models import (Document, DocumentVersion, LotShipment, LotSpecification,
                     TechnicalDocumentIndex, UserLotWorkflow)
from .search_service import store_lot
from .specification_service import process_specification, queue_specification, synchronize_specification
from .test_index import row


@override_settings(STORAGES={
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
})
class DocumentUpdateTests(TestCase):
    def setUp(self):
        self.owner=get_user_model().objects.create_user('document-owner')
        self.other=get_user_model().objects.create_user('document-other')
        self.lot=store_lot(row(987))
        self.lot.refresh_state='done';self.lot.checked_at=timezone.now();self.lot.save()
        self.doc=Document.objects.create(announcement=self.lot.announcement,lot=self.lot,
            source_id='public-tech',kind='technical',status='ready',name='spec.txt',
            url='https://example.test/spec')
        self.old=self.version('a','Old public specification')
        self.index=TechnicalDocumentIndex.objects.create(lot=self.lot,state='ready',checked_at=timezone.now())
        self.client.force_login(self.owner)

    def version(self, digest, text):
        version=DocumentVersion.objects.create(document=self.doc,sha256=digest*64,text=text)
        Document.objects.filter(pk=self.doc.pk).update(current_version=version)
        return version

    def test_both_owners_receive_current_shared_version_without_source_requests(self):
        with patch('tender_search.core.tasks.extract_lot_specification.delay') as task, \
                patch('tender_search.core.specification_service.lot_client') as source:
            first=queue_specification(self.owner,self.lot)
            self.version('b','New public specification')
            second=queue_specification(self.other,self.lot)
            first=queue_specification(self.owner,self.lot)
        self.assertEqual(first.documents,second.documents)
        self.assertEqual(first.documents[0]['text'],'New public specification')
        self.assertEqual(first.shared_revision,second.shared_revision)
        task.assert_not_called();source.assert_not_called()

    def test_on_demand_extraction_publishes_complete_cache_without_background_index(self):
        from types import SimpleNamespace
        self.index.delete()
        job=LotSpecification.objects.create(owner=self.owner,lot=self.lot,state='queued',generation='extract')
        file=SimpleNamespace(name='spec.txt',group_name='Техническая спецификация',
            url=self.doc.url,lot_id=self.lot.source_id)
        with patch('tender_search.core.specification_service.lot_client') as source, \
                patch('tender_search.core.technical_documents.cache_file',return_value=self.old):
            source.return_value.__enter__.return_value.documents.return_value=[file]
            process_specification(job.pk,'extract')
        self.assertEqual(TechnicalDocumentIndex.objects.get(lot=self.lot).state,'ready')
        with patch('tender_search.core.tasks.extract_lot_specification.delay') as task:
            second=queue_specification(self.other,self.lot)
        task.assert_not_called()
        self.assertEqual(second.documents[0]['text'],'Old public specification')

    def test_existing_snapshot_updates_on_page_and_poll_and_handles_version_revert(self):
        job=queue_specification(self.owner,self.lot)
        endpoint=f'/lots/{self.lot.pk}/status/'
        before=self.client.get(endpoint).json()['revisions']
        self.version('b','New public specification')
        response=self.client.get(endpoint,before).json()
        self.assertIn('New public specification',response['html'])
        self.assertNotEqual(before['specification'],response['revisions']['specification'])
        with CaptureQueriesContext(connection) as queries:
            unchanged=self.client.get(endpoint,response['revisions']).json()
        self.assertNotIn('html',unchanged)
        self.assertFalse(any('"core_lotspecification"."documents"' in q['sql'] or
                             '"core_documentversion"."text"' in q['sql'] for q in queries))
        Document.objects.filter(pk=self.doc.pk).update(current_version=self.old)
        page=self.client.get(f'/lots/{self.lot.pk}/')
        self.assertContains(page,'Old public specification')
        self.assertNotContains(page,'New public specification')
        job.refresh_from_db();self.assertEqual(job.documents[0]['text'],'Old public specification')

    def test_legacy_snapshots_update_but_do_not_create_another_owners_snapshot(self):
        job=LotSpecification.objects.create(owner=self.owner,lot=self.lot,state='done',
            documents=[{'text':'Obsolete legacy snapshot'}])
        self.assertContains(self.client.get(f'/lots/{self.lot.pk}/'),'Old public specification')
        job.refresh_from_db();self.assertTrue(job.shared_revision)
        self.client.force_login(self.other)
        self.assertNotContains(self.client.get(f'/lots/{self.lot.pk}/'),'Old public specification')
        self.assertFalse(LotSpecification.objects.filter(owner=self.other).exists())

    def test_pre_index_snapshot_updates_known_file_and_preserves_uncached_files(self):
        self.index.delete()
        job=LotSpecification.objects.create(owner=self.owner,lot=self.lot,state='done',documents=[
            {'name':self.doc.name,'source_url':self.doc.url,'scope':'lot','text':'Older snapshot'},
            {'name':'Uncached file','source_url':'https://example.test/missing','scope':'lot','text':'Keep this text'},
        ])
        self.version('b','Updated public text')
        Document.objects.filter(pk=self.doc.pk).update(url='https://example.test/new-version')
        synchronize_specification(job,self.lot)
        self.assertEqual(job.documents[0]['text'],'Updated public text')
        self.assertEqual(job.documents[1]['text'],'Keep this text')
        self.assertEqual(job.state,'done')
        self.assertTrue(job.shared_revision)
        job=LotSpecification.objects.defer('documents').get(pk=job.pk)
        with CaptureQueriesContext(connection) as queries:
            synchronize_specification(job,self.lot)
        self.assertFalse(any('"core_lotspecification"."documents"' in q['sql'] or
                             '"core_documentversion"."text"' in q['sql'] for q in queries))

    def test_incomplete_shared_update_and_inflight_personal_refresh_keep_saved_text(self):
        job=queue_specification(self.owner,self.lot)
        self.version('b','New public specification')
        self.index.state='partial';self.index.save()
        synchronize_specification(job,self.lot)
        self.assertEqual(job.documents[0]['text'],'Old public specification')
        self.index.state='ready';self.index.save()
        with patch('tender_search.core.tasks.extract_lot_specification.delay'):
            job=queue_specification(self.owner,self.lot,refresh=True)
        generation=job.generation
        synchronize_specification(job,self.lot)
        job.refresh_from_db()
        self.assertEqual(job.state,'queued');self.assertEqual(job.generation,generation)
        self.assertEqual(job.documents[0]['text'],'Old public specification')

    def test_stale_reader_cannot_overwrite_a_new_refresh_generation(self):
        job=queue_specification(self.owner,self.lot)
        self.version('b','New public specification')
        with patch('tender_search.core.tasks.extract_lot_specification.delay'):
            current=queue_specification(self.owner,self.lot,refresh=True)
        synchronize_specification(job,self.lot)
        current.refresh_from_db()
        self.assertEqual(current.state,'queued')
        self.assertEqual(current.documents[0]['text'],'Old public specification')

    def test_old_complete_index_can_update_saved_snapshot_but_empty_version_cannot(self):
        job=queue_specification(self.owner,self.lot)
        self.index.checked_at=timezone.now()-timedelta(days=2);self.index.save()
        version=self.version('b','Updated cached specification')
        synchronize_specification(job,self.lot)
        self.assertEqual(job.documents[0]['text'],'Updated cached specification')
        version.text='';version.save()
        synchronize_specification(job,self.lot)
        self.assertEqual(job.documents[0]['text'],'Updated cached specification')

    def test_private_act_cleanup_only_uses_owner_and_keeps_other_owners_act(self):
        old=timezone.now()-timedelta(days=120)
        self.lot.announcement.deadline=old;self.lot.announcement.save()
        UserLotWorkflow.objects.create(owner=self.owner,lot=self.lot,stage='archived',archived_at=old)
        UserLotWorkflow.objects.create(owner=self.other,lot=self.lot,stage='delivery')
        with TemporaryDirectory() as directory, override_settings(MEDIA_ROOT=directory):
            own=LotShipment.objects.create(owner=self.owner,lot=self.lot,act_uploaded_at=old)
            own.act.save('own.pdf',ContentFile(b'owner act'))
            other=LotShipment.objects.create(owner=self.other,lot=self.lot,act_uploaded_at=old)
            other.act.save('other.pdf',ContentFile(b'other act'))
            own_path=Path(own.act.path);other_path=Path(other.act.path)
            self.assertFalse(private_act_needed(own))
            self.assertTrue(private_act_needed(other))
            self.assertTrue(documents_needed([self.lot.pk],old,'technical'))
            cache.delete('document-retention-hour')
            cleanup_expired_documents()
            own.refresh_from_db();other.refresh_from_db()
            self.assertFalse(own.act);self.assertFalse(own_path.exists())
            self.assertTrue(other.act);self.assertTrue(other_path.exists())
            self.doc.refresh_from_db();self.assertEqual(self.doc.status,'ready')

    def test_recent_upload_survives_old_archive_or_missing_own_workflow(self):
        old=timezone.now()-timedelta(days=120)
        self.lot.announcement.deadline=old;self.lot.announcement.save()
        workflow=UserLotWorkflow.objects.create(owner=self.owner,lot=self.lot,stage='archived',archived_at=old)
        shipment=LotShipment.objects.create(owner=self.owner,lot=self.lot,act_uploaded_at=timezone.now())
        self.assertTrue(private_act_needed(shipment))
        workflow.delete()
        self.assertTrue(private_act_needed(shipment))
        shipment.act_uploaded_at=old
        self.assertFalse(private_act_needed(shipment))
