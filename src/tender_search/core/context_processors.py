from django.conf import settings
from .models import Favorite, TelegramSubscription, CompanySettings
from .lot_state import collection_lot_count
from django.utils.functional import SimpleLazyObject


def account_access(request):
    from .access import remaining_days
    access = getattr(request, 'trial_access', None)
    return {'access_days_left': remaining_days(access.expires_at) if access else None}


def favorite_counts(request):
    from .user_admin import can_manage_users
    return {"favorite_count": SimpleLazyObject(lambda: Favorite.objects.filter(owner=request.user).count()) if request.user.is_authenticated else 0,
            "company_bin_missing": SimpleLazyObject(lambda: not CompanySettings.objects.filter(owner=request.user).exclude(company_bin='').exists()) if request.user.is_authenticated else False,
            "collection_count": SimpleLazyObject(lambda: request._workspace_lot_count if hasattr(request, '_workspace_lot_count') else collection_lot_count(request.user)) if request.user.is_authenticated else 0,
            "telegram_connected": SimpleLazyObject(lambda: TelegramSubscription.objects.filter(owner=request.user, enabled=True, chat_id__isnull=False).exists()) if request.user.is_authenticated else False,
            "show_lead_collection": settings.LEAD_COLLECTION_AVAILABLE and request.user.is_staff,
            "can_manage_users": can_manage_users(request.user)}
def procurement_coverage(request):
    from .user_admin import can_manage_users
    if not can_manage_users(request.user) or not request.path.startswith('/search/'):
        return {}
    from .coverage_snapshot import display_snapshot
    # Fragments that omit the block do not even read the snapshot.
    snapshot = SimpleLazyObject(display_snapshot)
    return {key: SimpleLazyObject(lambda key=key: snapshot.get(key)) for key in (
        'index_coverage', 'medical_coverage', 'platform_statuses',
        'coverage_pending', 'coverage_stale', 'coverage_measured_at')}
