"""Shared database throttles: no dependence on per-process caches or forwarded-for."""
import ipaddress
from functools import wraps
from math import ceil

from django.conf import settings
from django.db import transaction
from django.shortcuts import render
from django.utils import timezone
from django.utils.crypto import salted_hmac
from django.utils.cache import patch_cache_control

from .models import AuthRateBucket, EmailVerification


def client_ip(request):
    value = request.META.get('REMOTE_ADDR', 'unknown')
    # Enable only on the isolated web service reachable through our CF tunnel.
    if settings.AUTH_TRUST_CLOUDFLARE_IP:
        value = request.META.get('HTTP_CF_CONNECTING_IP', value)
    try:
        address = ipaddress.ip_address(value)
        if address.version == 6:
            return str(ipaddress.ip_network(f'{address}/64', strict=False))
        return str(address)
    except ValueError:
        return 'unknown'


def needs_verification(user):
    if not user.is_authenticated or user.is_superuser:
        return False
    record = EmailVerification.objects.filter(owner=user, required=True).first()
    return bool(record and (record.verified_at is None or record.email != user.email.strip().lower()))


def limits(request, action):
    ip = client_ip(request)
    account = (request.POST.get('username') if action == 'login' else request.POST.get('email')) or ''
    account = account.strip().casefold()[:254]
    if action == 'login':
        return [(f'login-ip:{ip}', 30, 900), (f'login-pair:{ip}:{account}', 5, 900), (f'login-account:{account}', 50, 3600)]
    if action == 'register':
        return [(f'register-ip:{ip}', 5, 3600)]
    if action == 'reset':
        return [(f'reset-ip:{ip}', 10, 3600), (f'reset-email:{account}', 3, 3600)]
    if action == 'verify-resend':
        return [(f'verify-ip:{ip}', 10, 3600), (f'verify-user:{request.user.pk}', 3, 3600)]
    return [(f'{action}-ip:{ip}', 30, 900)]


def consume_limits(rules):
    now = timezone.now()
    retry = 0
    # Deterministic lock order prevents deadlocks across concurrent requests.
    rules = sorted((salted_hmac('auth-rate', key, algorithm='sha256').hexdigest(), limit, seconds)
                   for key, limit, seconds in rules)
    with transaction.atomic():
        records = []
        for key, limit, seconds in rules:
            AuthRateBucket.objects.get_or_create(key=key)
            record = AuthRateBucket.objects.select_for_update().get(pk=key)
            elapsed = (now-record.started_at).total_seconds()
            if elapsed >= seconds:
                record.started_at, record.attempts, elapsed = now, 0, 0
            if record.attempts >= limit:
                retry = max(retry, ceil(seconds-elapsed))
            records.append(record)
        if not retry:
            for record in records:
                record.attempts += 1
                record.save(update_fields=['started_at', 'attempts'])
    return retry


def rate_limited(action):
    def decorate(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if request.method == 'POST':
                retry = consume_limits(limits(request, action))
                if retry:
                    response = render(request, 'public/auth_notice.html', {
                        'notice_title': 'Слишком много попыток',
                        'notice_text': f'Подождите примерно {max(1, ceil(retry/60))} мин. и попробуйте снова.',
                    }, status=429)
                    response['Retry-After'] = str(retry)
                    return response
            return view(request, *args, **kwargs)
        return wrapped
    return decorate


class AuthResponseMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        user=getattr(request,'user',None)
        if user is not None and user.is_authenticated:
            patch_cache_control(response,private=True,no_store=True)
        if request.path.startswith(('/login/', '/register/', '/password/', '/account/email/')):
            response['Cache-Control'] = 'no-store, private'
            # Keep an origin for browser form POSTs and Django's HTTPS referer
            # check, while never disclosing token-bearing paths in Referer.
            response['Referrer-Policy'] = 'strict-origin'
        return response


