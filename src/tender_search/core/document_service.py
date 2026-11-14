from datetime import timedelta
from pathlib import Path
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from tender_search.client import SourceError
from .models import Announcement, Document, DocumentVersion, Favorite, Lot, LotDocumentDownload


def stored_path(value):
    root = Path(settings.MEDIA_ROOT).resolve()
    path = Path(value).resolve()
    if not path.is_relative_to(root):
        raise ValueError('Invalid document path')
    return path


def queue_documents(lot_id):
    from .tasks import download_favorite_documents
    if not Favorite.objects.filter(lot_id=lot_id).exists():
        return
    batch, created = LotDocumentDownload.objects.get_or_create(lot_id=lot_id)
    if not created and (batch.state == 'done' or (batch.state in {'queued', 'running'} and batch.updated_at > timezone.now()-timedelta(minutes=25))):
        return
    LotDocumentDownload.objects.filter(pk=batch.pk).update(state='queued', error='', updated_at=timezone.now())
    try:
        download_favorite_documents.delay(lot_id)
    except Exception:
        LotDocumentDownload.objects.filter(pk=batch.pk).update(state='error',error='Очередь недоступна. Повторите загрузку.')


def contract_file(file):
    import re
    label = f"{getattr(file, 'group_name', '')} {file.name}"
    return bool(re.search(r'договор|шарт|contract', label, re.I))


def download_documents(lot_id, api):
    if not LotDocumentDownload.objects.filter(lot_id=lot_id,state='queued').update(state='running',updated_at=timezone.now()):
        return
    lot = Lot.objects.select_related('announcement').get(pk=lot_id)
    errors = 0
    try:
        if not Favorite.objects.filter(lot=lot).exists():
            return
        files = api.documents(lot.announcement.source_id)
        for file in files:
            if file.lot_id not in {None, lot.source_id} or not contract_file(file):
                continue
            if not Favorite.objects.filter(lot=lot).exists():
                break
            previous = Document.objects.filter(announcement=lot.announcement, source_id=file.id).first()
            unchanged = previous is not None and previous.url == file.url
            doc, _ = Document.objects.update_or_create(announcement=lot.announcement,source_id=file.id,
                defaults={'lot':lot if file.lot_id else None,'name':file.name,'url':file.url,'kind':'contract'})
            cached = doc.current_version if doc.current_version_id else doc.versions.order_by('-pk').first()
            if unchanged and cached and cached.storage_path and stored_path(cached.storage_path).is_file():
                Document.objects.filter(pk=doc.pk).update(status='ready',error='')
                continue
            try:
                Document.objects.filter(pk=doc.pk).update(status='downloading',error='')
                result = api.download(file,Path(settings.MEDIA_ROOT))
                version, _ = DocumentVersion.objects.update_or_create(document=doc,sha256=result['sha256'],defaults={'storage_path':result['path']})
                Document.objects.filter(pk=doc.pk).update(current_version=version,status='ready',error='')
            except Exception as exc:
                errors += 1
                Document.objects.filter(pk=doc.pk).update(status='error',error=str(exc)[:200] if isinstance(exc,SourceError) else 'Не удалось скачать файл.')
        LotDocumentDownload.objects.filter(lot=lot).update(state='partial' if errors else 'done',error='Некоторые файлы не удалось скачать. Повторите загрузку.' if errors else '',updated_at=timezone.now())
    except Exception as exc:
        LotDocumentDownload.objects.filter(lot=lot).update(state='error',error=str(exc)[:200] if isinstance(exc,SourceError) else 'Не удалось получить документы.',updated_at=timezone.now())
    finally:
        cleanup_documents(lot.announcement_id)


def documents_needed(lot_ids, deadline, kind, now=None, *, owner_id=None):
    """Shared files consider all workflows; personal files only their owner's."""
    from .models import UserLotWorkflow
    now = now or timezone.now()
    if deadline is None or deadline > now - timedelta(days=7):
        return True
    workflows=UserLotWorkflow.objects.filter(lot_id__in=lot_ids)
    if owner_id is not None:
        workflows=workflows.filter(owner_id=owner_id)
    for workflow in workflows:
        if workflow.stage not in ('lost', 'not_participated', 'archived'):
            return True
        days = 90 if kind in ('contract', 'act') or workflow.stage == 'archived' else 30
        changed = workflow.completed_at or workflow.archived_at or workflow.updated_at
        if changed > now - timedelta(days=days):
            return True
    return False


def private_act_needed(shipment, now=None):
    now=now or timezone.now()
    uploaded=shipment.act_uploaded_at or shipment.shipped_at or shipment.created_at
    # A recently uploaded personal file gets its own retention period even if
    # its owner's workflow has already been archived or removed.
    return uploaded>now-timedelta(days=90) or documents_needed(
        [shipment.lot_id],shipment.lot.announcement.deadline,'act',now,
        owner_id=shipment.owner_id)


def cleanup_documents(announcement_id):
    paths = []
    with transaction.atomic():
        announcement = Announcement.objects.select_for_update().filter(pk=announcement_id).first()
        if not announcement:
            return
        for doc in Document.objects.filter(announcement_id=announcement_id):
            lot_ids = [doc.lot_id] if doc.lot_id else list(announcement.lots.values_list('pk', flat=True))
            if documents_needed(lot_ids, announcement.deadline, doc.kind):
                continue
            paths.extend(doc.versions.exclude(storage_path='').values_list('storage_path', flat=True))
            doc.versions.update(storage_path='', text='', pages=[])
            Document.objects.filter(pk=doc.pk).update(status='expired', error='')
        from .models import LotSpecification
        for lot in announcement.lots.all():
            if not documents_needed([lot.pk], announcement.deadline, 'technical'):
                LotSpecification.objects.filter(lot=lot).update(documents=[], state='not_found', error='Срок хранения документов истёк.')
        def remove_files():
            for value in set(paths):
                if not DocumentVersion.objects.filter(storage_path=value).exists():
                    try:
                        stored_path(value).unlink(missing_ok=True)
                    except (OSError, ValueError):
                        pass
        transaction.on_commit(remove_files)


def cleanup_expired_documents():
    from django.core.cache import cache
    from .models import LotShipment
    if not cache.add('document-retention-hour', True, timeout=3600):
        return
    ids = Document.objects.exclude(status='expired').values_list('announcement_id', flat=True).distinct()
    for announcement_id in ids.iterator():
        cleanup_documents(announcement_id)
    for shipment in LotShipment.objects.exclude(act='').select_related('lot__announcement').iterator():
        if not private_act_needed(shipment):
            name, storage = shipment.act.name, shipment.act.storage
            LotShipment.objects.filter(pk=shipment.pk, act=name).update(act='')
            if not LotShipment.objects.filter(act=name).exists():
                storage.delete(name)
