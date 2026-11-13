"""Shared technical-specification cache, independent of personal favorites."""
from datetime import timedelta
from pathlib import Path
from django.conf import settings
from django.db import transaction
from django.db.models import Q, F, OuterRef, Subquery
from django.utils import timezone
from .models import Document, DocumentVersion, TechnicalDocumentIndex, IndexedLot, MedicalIndexedLot
from .document_service import stored_path


@transaction.atomic
def cache_file(lot, file, api):
    from .specification_service import extract_text
    from .models import Announcement
    Announcement.objects.select_for_update().get(pk=lot.announcement_id)
    previous = Document.objects.filter(announcement=lot.announcement, source_id=file.id).first()
    unchanged = previous is not None and previous.url == file.url
    doc, _ = Document.objects.update_or_create(announcement=lot.announcement, source_id=file.id,
        defaults={'lot': lot if file.lot_id else None, 'name': file.name, 'url': file.url, 'kind': 'technical'})
    version = doc.current_version if doc.current_version_id else doc.versions.order_by('-pk').first()
    if unchanged and version and version.text and version.storage_path and stored_path(version.storage_path).is_file():
        if version.search_text is None:
            version.save(update_fields=['text'])
        return version
    result = api.download(file, Path(settings.MEDIA_ROOT), max_bytes=20*1024*1024)
    version, _ = DocumentVersion.objects.update_or_create(document=doc, sha256=result['sha256'],
        defaults={'storage_path': str(result['path'])})
    text, truncated = extract_text(result['path'], file.name)
    version.text = text
    version.pages = [{'truncated': truncated}]
    version.save(update_fields=['text', 'pages', 'updated_at'])
    Document.objects.filter(pk=doc.pk).update(current_version=version, status='ready', error='')
    return version


def process(index_id):
    from .source import lot_client
    from .specification_service import technical_file
    if not TechnicalDocumentIndex.objects.filter(pk=index_id, state='queued').update(state='running', updated_at=timezone.now()):
        return
    index = TechnicalDocumentIndex.objects.select_related('lot__announcement').get(pk=index_id)
    errors = 0
    try:
        with lot_client(index.lot_id) as api:
            files = [f for f in api.documents(index.lot.announcement.source_id)
                     if f.lot_id in (None, index.lot.source_id) and technical_file(f)]
            for file in files:
                try:
                    cache_file(index.lot, file, api)
                except Exception:
                    errors += 1
        state = 'partial' if errors else ('ready' if files else 'not_found')
        error = 'Некоторые техспецификации не удалось прочитать.' if errors else ''
    except Exception:
        state, error = 'error', 'Источник техспецификаций временно недоступен.'
    TechnicalDocumentIndex.objects.filter(pk=index_id).update(state=state, error=error,
        checked_at=timezone.now(), updated_at=timezone.now())


def schedule():
    """Bound each tick; failed and stale jobs retry without blocking registry refresh."""
    from .search_service import store_lot
    from .tasks import index_technical_documents
    now = timezone.now()
    update_progress()
    # Keep a small queue: document traffic shares the source rate limiter.
    active = TechnicalDocumentIndex.objects.filter(state__in=['queued', 'running'], updated_at__gt=now-timedelta(minutes=20))
    if active.exists():
        return
    for model, source in ((IndexedLot, 'zakup'), (MedicalIndexedLot, None)):
        rows = model.objects.filter(current=True, deadline__gt=now)
        if source:
            rows = rows.filter(status_id__in=[6, 7, 9], details_skipped=False)
            done = TechnicalDocumentIndex.objects.filter(lot__announcement__source=source,
                checked_at__gt=now-timedelta(days=1)).values('lot__source_id')
            rows = rows.exclude(source_id__in=done)
        else:
            recent = TechnicalDocumentIndex.objects.filter(lot__announcement__source=OuterRef('source'),
                lot__source_id=OuterRef('source_id'), checked_at__gt=now-timedelta(days=1))
            from django.db.models import Exists
            rows = rows.annotate(recently_checked=Exists(recent)).filter(recently_checked=False)
        for row in rows.order_by('source_id').values_list('row', flat=True)[:10]:
            lot = store_lot(row)
            index, _ = TechnicalDocumentIndex.objects.get_or_create(lot=lot)
            if index.checked_at and index.checked_at > now-timedelta(days=1):
                continue
            if TechnicalDocumentIndex.objects.filter(pk=index.pk).exclude(
                    state__in=['queued', 'running'], updated_at__gt=now-timedelta(minutes=20)).update(state='queued', updated_at=now):
                try:
                    index_technical_documents.delay(index.pk)
                except Exception:
                    TechnicalDocumentIndex.objects.filter(pk=index.pk).update(state='error')


def matching_lots(matcher, *, now=None, is_active=None):
    """Stream shared text; retain only matching identities in search memory."""
    from tender_search.matching import match_lot
    matched = {}
    latest = DocumentVersion.objects.filter(document_id=OuterRef('document_id')).order_by('-pk').values('pk')[:1]
    versions = DocumentVersion.objects.filter(document__kind='technical',
        document__announcement__deadline__gt=now or timezone.now()).filter(
            Q(pk=F('document__current_version_id')) |
            Q(document__current_version__isnull=True, pk=Subquery(latest))).exclude(text='')
    if settings.SEARCH_DB_PREFILTERS and not matcher.number_only:
        from .query_search import text_candidates
        versions = versions.filter(text_candidates(matcher))
    # Do not transfer raw announcement/lot JSON, file paths or page metadata.
    values = versions.values_list('text', 'document__announcement__source',
        'document__announcement__source_id', 'document__lot__source_id')
    for count, (text, source, announcement_id, lot_id) in enumerate(values.iterator(chunk_size=50)):
        if count % 50 == 0 and is_active and not is_active():
            return None
        result = match_lot('', text, matcher)
        if result:
            key = (source, announcement_id, lot_id)
            matched[key] = result
    return matched


def update_progress():
    """Record the first bounded population, including terminal missing files."""
    from django.core.cache import cache
    from .models import TechnicalIndexProgress
    if not cache.add('technical-progress-minute', True, timeout=60):
        return
    now = timezone.now()
    active = {f'zakup:{sid}' for sid in IndexedLot.objects.filter(current=True,
        deadline__gt=now,status_id__in=[6,7,9],details_skipped=False).values_list('source_id',flat=True)}
    active.update(f'{source}:{sid}' for source,sid in MedicalIndexedLot.objects.filter(
        current=True,deadline__gt=now).values_list('source','source_id'))
    progress = TechnicalIndexProgress.objects.first()
    if progress is None:
        if not active:
            return
        progress = TechnicalIndexProgress.objects.create(started_at=now,targets=sorted(active))
    states = {f'{source}:{sid}':state for source,sid,state in TechnicalDocumentIndex.objects.values_list(
        'lot__announcement__source','lot__source_id','state')}
    targets=set(progress.targets)
    retired=targets-active
    checked=sum(states.get(key) in ('ready','not_found') for key in targets & active)
    errors=sum(states.get(key) in ('error','partial') for key in targets & active)
    readable=sum(states.get(key)=='ready' for key in targets & active)
    missing=sum(states.get(key)=='not_found' for key in targets & active)
    progress.counts={'total':len(targets),'checked':checked,'retired':len(retired),
        'errors':errors,'readable':readable,'missing':missing,
        'remaining':len(targets)-checked-len(retired)}
    if not progress.completed_at and checked+len(retired)==len(targets):
        progress.completed_at=now
    progress.save()
