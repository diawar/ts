"""On-demand technical specifications inside a lot card. No favorite or Telegram side effects."""
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from io import BytesIO
from zipfile import ZipFile, BadZipFile
from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException
import re
import secrets
from hashlib import sha256
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from pypdf import PdfReader
from tender_search.client import SourceError
from .models import LotSpecification,Document,TechnicalDocumentIndex
from .source import lot_client
from .document_service import stored_path

MAX_TEXT=160000


def technical_file(file):
    label=((file.group_name or '')+' '+file.name).casefold()
    return bool(re.search(r'технич|техспец|тех[.\s_-]*спец|техникалық|ерекшелік|technical[\s_-]*spec',label))


def extract_text(path, name):
    path=Path(path)
    data=path.read_bytes()
    if len(data)>20*1024*1024:raise SourceError('Файл техспецификации больше 20 МБ.')
    if data.startswith(b'%PDF-'):
        reader=PdfReader(BytesIO(data))
        if reader.is_encrypted or len(reader.pages)>80:raise SourceError('PDF зашифрован или содержит более 80 страниц.')
        text='\n\n'.join(page.extract_text(extraction_mode='layout') or '' for page in reader.pages)
    elif data.startswith(b'PK'):
        with ZipFile(BytesIO(data)) as archive:
            infos=archive.infolist()
            if len(infos)>1000 or sum(info.file_size for info in infos)>30*1024*1024:
                raise SourceError('Офисный документ слишком большой после распаковки.')
            if 'word/document.xml' not in archive.namelist():
                raise SourceError('Поддерживается текст PDF, DOCX и TXT. Этот архив не является DOCX.')
            try:
                root=ElementTree.fromstring(archive.read('word/document.xml'),forbid_dtd=True)
            except DefusedXmlException as exc:
                raise SourceError('DOCX содержит запрещённые XML-объявления.') from exc
            ns={'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
            text='\n'.join(''.join(p.itertext()) for p in root.findall('.//w:p',ns))
    elif name.casefold().endswith('.txt'):
        try:text=data.decode('utf-8-sig')
        except UnicodeDecodeError:text=data.decode('cp1251')
    else:raise SourceError('Поддерживается текст PDF, DOCX и TXT. Для этого формата требуется конвертация.')
    text=text.strip()
    if not text:raise SourceError('Текст не извлечён. Возможно, документ является сканом и требует OCR.')
    return text[:MAX_TEXT],len(text)>MAX_TEXT


def shared_revision(lot):
    """Compare shared versions without transferring extracted text on every poll."""
    index_state=TechnicalDocumentIndex.objects.filter(lot_id=lot.pk).values_list('state',flat=True).first()
    if index_state not in (None,'ready'):
        return ''
    rows=list(Document.objects.filter(announcement_id=lot.announcement_id,kind='technical').filter(
        Q(lot_id=lot.pk)|Q(lot__isnull=True)).order_by('pk').values_list(
            'pk','lot_id','name','url','status','current_version_id','current_version__updated_at')[:9])
    if not rows or len(rows)>8 or any(row[4]!='ready' or not row[5] for row in rows):
        return ''
    return sha256(repr((index_state,rows)).encode()).hexdigest()


def synchronize_specification(job, lot):
    """Refresh an existing snapshot from complete public cache, without source I/O."""
    if not job or job.state not in ('done','partial'):
        return job
    revision=shared_revision(lot)
    if not revision or revision==job.shared_revision:
        return job
    complete=TechnicalDocumentIndex.objects.filter(lot_id=lot.pk,state='ready').exists()
    documents=cached_documents(lot,require_fresh=False,require_index=complete)
    if documents and not complete:
        # Legacy snapshots predate the shared index. Only replace known files;
        # an unindexed cache cannot prove that their whole file list is complete.
        merged=[]
        for saved in job.documents:
            candidates=[doc for doc in documents if doc['scope']==saved.get('scope') and (
                doc['document_id']==saved.get('document_id') if saved.get('document_id') else
                doc['source_url']==saved.get('source_url'))]
            if not candidates and saved.get('name'):
                candidates=[doc for doc in documents if
                    doc['scope']==saved.get('scope') and doc['name']==saved['name']]
            merged.append(candidates[0] if len(candidates)==1 else saved)
        documents=merged
    # A writer may change a pointer during the read. Keep the last good snapshot
    # and retry on the next request instead of recording a mixed revision.
    if not documents or revision!=shared_revision(lot):
        return job
    changed=LotSpecification.objects.filter(pk=job.pk,generation=job.generation,
        state=job.state,updated_at=job.updated_at).update(documents=documents,
            shared_revision=revision,state='done' if complete else job.state,
            error='' if complete else job.error,updated_at=timezone.now())
    if changed:
        job.refresh_from_db()
    return job


def cached_documents(lot, *, require_fresh=True, require_index=True):
    # Only a complete shared index proves that no required file is missing.
    index=TechnicalDocumentIndex.objects.filter(lot=lot,state='ready')
    if require_fresh:
        index=index.filter(checked_at__gt=timezone.now()-timedelta(days=1))
    if require_index and not index.exists():
        return []
    files=list(Document.objects.filter(announcement_id=lot.announcement_id,kind='technical').filter(
        Q(lot_id=lot.pk)|Q(lot__isnull=True)).select_related('current_version').order_by('pk')[:9])
    if not files or len(files)>8:return []
    result=[]
    for file in files:
        version=file.current_version
        if file.status!='ready' or not version or not version.text:return []
        result.append({'document_id':file.pk,'name':file.name,'text':version.text,'source_url':file.url,
                       'scope':'lot' if file.lot_id else 'announcement',
                       'truncated':bool(version.pages and version.pages[0].get('truncated'))})
    return result


def queue_specification(owner, lot, *, refresh=False):
    from .tasks import extract_lot_specification
    with transaction.atomic():
        job,_=LotSpecification.objects.get_or_create(owner=owner,lot=lot)
        job=LotSpecification.objects.select_for_update().get(pk=job.pk)
        if job.state in ('queued','running') and job.generation and job.updated_at>timezone.now()-timedelta(minutes=15):return job
        if not refresh:
            if job.state=='done' and job.documents:
                return synchronize_specification(job,lot)
            revision=shared_revision(lot)
            documents=cached_documents(lot)
            if documents and revision and revision==shared_revision(lot):
                job.documents=documents;job.state='done';job.error='';job.generation=secrets.token_hex(16)
                job.shared_revision=revision
                job.save(update_fields=['documents','state','error','generation','shared_revision','updated_at'])
                return job
        generation=secrets.token_hex(16)
        job.state='queued';job.generation=generation;job.error=''
        job.shared_revision=''
        job.save(update_fields=['state','generation','error','shared_revision','updated_at'])
    try:extract_lot_specification.delay(job.pk,generation)
    except Exception:
        LotSpecification.objects.filter(pk=job.pk,generation=generation).update(state='error',error='Очередь извлечения недоступна. Повторите позже.')
        job.refresh_from_db()
    return job


def process_specification(job_id, generation):
    if not LotSpecification.objects.filter(pk=job_id,generation=generation,state='queued').update(state='running',updated_at=timezone.now()):return
    job=LotSpecification.objects.select_related('lot__announcement').get(pk=job_id)
    documents=[];errors=[]
    try:
        with lot_client(job.lot_id) as api:
            files=[file for file in api.documents(job.lot.announcement.source_id) if file.lot_id in (None,job.lot.source_id) and technical_file(file)]
            if not files:
                TechnicalDocumentIndex.objects.update_or_create(lot_id=job.lot_id,
                    defaults={'state':'not_found','checked_at':timezone.now(),'error':''})
                LotSpecification.objects.filter(pk=job_id,generation=generation).update(state='not_found',error='Связанная техническая спецификация не найдена среди файлов портала.',updated_at=timezone.now());return
            work=Path(settings.MEDIA_ROOT)/'specification-work';work.mkdir(parents=True,exist_ok=True)
            with TemporaryDirectory(dir=work) as temporary:
                for file in files[:8]:
                    try:
                        from .technical_documents import cache_file
                        version=cache_file(job.lot,file,api)
                        text=version.text
                        truncated=bool(version.pages and version.pages[0].get('truncated'))
                        documents.append({'document_id':version.document_id,'name':file.name,'text':text,'source_url':file.url,'scope':'lot' if file.lot_id else 'announcement','truncated':truncated})
                    except Exception as exc:
                        errors.append(str(exc)[:200] if isinstance(exc,SourceError) else 'Не удалось извлечь текст одного из документов.')
                if len(files)>8:errors.append('Показаны первые 8 технических документов.')
        # An on-demand extraction also checks the complete public file list.
        # Publish that result so other owners do not wait for the indexer's turn;
        # a partial extraction must not masquerade as a complete shared cache.
        TechnicalDocumentIndex.objects.update_or_create(lot_id=job.lot_id,
            defaults={'state':'partial' if errors else 'ready',
                      'checked_at':timezone.now(),'error':' '.join(errors)[:300]})
        LotSpecification.objects.filter(pk=job_id,generation=generation).update(documents=documents,
            state='partial' if documents and errors else ('done' if documents else 'error'),error=' '.join(errors)[:300],updated_at=timezone.now())
    except Exception as exc:
        LotSpecification.objects.filter(pk=job_id,generation=generation).update(state='error',error=str(exc)[:300] if isinstance(exc,SourceError) else 'Не удалось получить техническую спецификацию.',updated_at=timezone.now())
