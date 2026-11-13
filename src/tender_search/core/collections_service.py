"""Refresh saved filters locally, independently of Telegram and working hours."""
from .access import eligible_user_ids
from datetime import timedelta
import uuid
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from .models import SearchProfile, CollectionLot, WebSearch, SearchHit, Favorite, AnnouncementWatch


def enqueue_collection(profile_id, *, force=False):
    from .tasks import refresh_collection
    now = timezone.now()
    with transaction.atomic():
        profile = SearchProfile.objects.select_for_update().filter(pk=profile_id, owner_id__in=eligible_user_ids()).first()
        if not profile or profile.filters.get('manual') or (not force and not profile.collection_enabled):
            return False
        if profile.collection_lease_until and profile.collection_lease_until > now:
            return False
        if not force and profile.collection_next_at and profile.collection_next_at > now:
            return False
        lease = uuid.uuid4().hex
        profile.collection_lease = lease
        profile.collection_lease_until = now+timedelta(minutes=25)
        profile.save(update_fields=['collection_lease', 'collection_lease_until'])
        revision = profile.collection_revision
    try:
        refresh_collection.delay(profile_id, lease, revision)
    except Exception:
        SearchProfile.objects.filter(pk=profile_id, collection_lease=lease).update(
            collection_lease='', collection_lease_until=None,
            collection_next_at=now+timedelta(minutes=1), collection_error='Очередь недоступна. Повторим обновление.')
        return False
    return True


def schedule_collections():
    now = timezone.now()
    profiles = SearchProfile.objects.filter(collection_enabled=True, owner_id__in=eligible_user_ids()).filter(
        Q(collection_next_at__isnull=True) | Q(collection_next_at__lte=now)).filter(
        Q(collection_lease_until__isnull=True) | Q(collection_lease_until__lte=now))
    for identifier in profiles.values_list('pk', flat=True)[:20]:
        enqueue_collection(identifier)


def update_collection(profile_id, lease, revision):
    from .index_service import execute_local_search
    from .workflow_state import inherit_workflows
    profile = SearchProfile.objects.filter(pk=profile_id, collection_lease=lease,
        collection_revision=revision, owner_id__in=eligible_user_ids()).first()
    if not profile:
        return
    job = None
    try:
        specification = profile.filters.get('specification')
        if not specification:
            raise ValueError('Missing saved filter')
        job = WebSearch.objects.create(owner=profile.owner,
            specification=dict(specification, collection_profile_id=profile.pk))
        execute_local_search(job.pk)
        job.refresh_from_db()
        if job.error or job.status not in ('done', 'partial'):
            raise ValueError('Search failed')
        with transaction.atomic():
            list(SearchProfile.objects.select_for_update().filter(owner_id=profile.owner_id).order_by('pk'))
            current = SearchProfile.objects.select_for_update().filter(pk=profile_id,
                collection_lease=lease, collection_revision=revision).first()
            if not current:
                return
            now = timezone.now()
            hits = list(job.hits.values('lot_id', 'terms'))
            old = {item.lot_id: item for item in CollectionLot.objects.filter(profile=current)}
            if not old:
                # Recover important lots from searches explicitly tied to this
                # saved filter, including already expired favorites.
                important = Q(lot_id__in=Favorite.objects.filter(owner=current.owner).values('lot_id')) | Q(
                    lot__announcement_id__in=AnnouncementWatch.objects.filter(owner=current.owner, enabled=True).values('announcement_id'))
                historical = SearchHit.objects.filter(search__owner=current.owner).filter(
                    Q(search__specification__profile_id=current.pk) | Q(search__specification__monitor_profile_id=current.pk)).filter(important)
                seeds = {}
                for hit in historical.order_by('-search__created_at').values('lot_id', 'terms'):
                    seeds.setdefault(hit['lot_id'], hit['terms'])
                CollectionLot.objects.bulk_create(inherit_workflows(current.owner_id, [CollectionLot(profile=current, lot_id=identifier,
                    terms=terms, matches_filter=False) for identifier, terms in seeds.items()]))
                old = {item.lot_id: item for item in CollectionLot.objects.filter(profile=current)}
            CollectionLot.objects.bulk_create(inherit_workflows(current.owner_id, [CollectionLot(profile=current, lot_id=h['lot_id'],
                terms=h['terms'], last_seen_at=now) for h in hits if h['lot_id'] not in old]))
            changed = []
            for hit in hits:
                if hit['lot_id'] in old and old[hit['lot_id']].terms != hit['terms']:
                    old[hit['lot_id']].terms = hit['terms']
                    changed.append(old[hit['lot_id']])
            CollectionLot.objects.bulk_update(changed, ['terms'])
            ids = [h['lot_id'] for h in hits]
            CollectionLot.objects.filter(profile=current, lot_id__in=ids).update(matches_filter=True, last_seen_at=now)
            # Incomplete coverage must not erase previously discovered matches.
            if job.complete:
                CollectionLot.objects.filter(profile=current).exclude(lot_id__in=ids).update(matches_filter=False)
            current.collection_updated_at = now
            current.collection_error = ''
            current.save(update_fields=['collection_updated_at', 'collection_error'])
    except Exception:
        SearchProfile.objects.filter(pk=profile_id, collection_lease=lease, collection_revision=revision).update(
            collection_error='Не удалось обновить подборку. Сохранённые лоты доступны; повторим позже.')
    finally:
        SearchProfile.objects.filter(pk=profile_id, collection_lease=lease).update(
            collection_lease='', collection_lease_until=None,
            collection_next_at=timezone.now()+timedelta(minutes=profile.interval_minutes))
        if job is not None:
            WebSearch.objects.filter(pk=job.pk).delete()

