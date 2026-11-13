"""Time-limited access shared by the website and personal background services."""
from datetime import timedelta
from math import ceil

from django.contrib.auth import get_user_model
from django.contrib.auth.views import redirect_to_login
from django.db.models import F, Q
from django.db.models.functions import Lower, Trim
from django.http import HttpResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone

TRIAL_DAYS = 14


def trial_end():
    return timezone.now() + timedelta(days=TRIAL_DAYS)


def remaining_days(expires_at, now=None):
    return max(0, ceil((expires_at - (now or timezone.now())).total_seconds() / 86400))


def eligible_user_ids():
    from .models import TrialAccess
    now = timezone.now()
    return get_user_model().objects.filter(is_active=True).filter(
        Q(is_superuser=True)
        | Q(pk__in=TrialAccess.objects.filter(expires_at__gt=now).values('owner_id'))
        | Q(trial_access__isnull=True, date_joined__gt=now-timedelta(days=TRIAL_DAYS))
    ).annotate(normalized_email=Lower(Trim('email'))).exclude(
        Q(is_superuser=False, email_verification__required=True)
        & (Q(email_verification__verified_at__isnull=True)
           | ~Q(email_verification__email=F('normalized_email')))
    ).values('pk')


def access_for(user):
    from .models import TrialAccess
    # Missing records start at registration, never at the current visit.
    return TrialAccess.objects.get_or_create(owner=user, defaults={
        'started_at': user.date_joined,
        'expires_at': user.date_joined + timedelta(days=TRIAL_DAYS),
    })[0]


class AccessMiddleware:
    public = {'landing', 'login', 'logout', 'register', 'health', 'admin_disabled', 'robots', 'sitemap', 'guides', 'guide', 'catalog', 'catalog_lot', 'catalog_category', 'catalog_region'}
    public |= {'password_reset', 'password_reset_done', 'password_reset_confirm', 'password_reset_complete',
               'email_notice', 'email_resend', 'email_confirm', 'account_identity'}

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response['X-TenderSearch-User'] = str(request.user.pk) if request.user.is_authenticated else 'anonymous'
        return response

    def process_view(self, request, view_func, view_args, view_kwargs):
        name = request.resolver_match.url_name
        expected = request.headers.get('X-TenderSearch-User')
        if not expected and request.method == 'POST':
            expected = request.POST.get('_account_id')
        actual = str(request.user.pk) if request.user.is_authenticated else 'anonymous'
        if expected and expected != actual:
            from django.http import JsonResponse
            return JsonResponse({'error': 'Аккаунт изменился. Откройте страницу заново.'}, status=409,
                headers={'HX-Refresh': 'true', 'Cache-Control': 'no-store, private'})
        if name in self.public:
            return None
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        if request.user.is_superuser:
            return None
        from .auth_security import needs_verification
        if needs_verification(request.user):
            destination = reverse('email_notice')
            if request.headers.get('HX-Request') == 'true':
                return HttpResponse(status=403, headers={'HX-Redirect': destination})
            return redirect(destination)
        request.trial_access = access_for(request.user)
        if name == 'account_access' or request.trial_access.expires_at > timezone.now():
            return None
        destination = reverse('account_access')
        if request.headers.get('HX-Request') == 'true':
            return HttpResponse(status=403, headers={'HX-Redirect': destination})
        return redirect(destination)
