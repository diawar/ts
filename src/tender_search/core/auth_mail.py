"""Durable auth mail queue. Reset requests do not look up accounts in HTTP views."""
from datetime import timedelta
from urllib.parse import urlsplit
import uuid

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core import signing
from django.core.exceptions import ImproperlyConfigured
from django.core.mail import send_mail
from django.db import transaction
from django.db.models.functions import Lower, Trim
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from .models import AuthMailJob, AuthRateBucket, EmailVerification

VERIFY_SALT = 'tendersearch-email-confirm-v1'


def auth_origin():
    origin = settings.AUTH_SITE_URL.rstrip('/')
    parsed = urlsplit(origin)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/')):
        raise ImproperlyConfigured('AUTH_SITE_URL must be a trusted http(s) origin')
    return origin


def queue_verification(user):
    with transaction.atomic():
        record, _ = EmailVerification.objects.get_or_create(owner=user, defaults={'email': user.email.strip().lower(), 'required': False})
        record = EmailVerification.objects.select_for_update().get(pk=record.pk)
        record.email = user.email.strip().lower()
        record.nonce = uuid.uuid4().hex
        record.verified_at = None
        record.save(update_fields=['email', 'nonce', 'verified_at'])
        AuthMailJob.objects.create(kind='verify', owner=user, email=record.email,
                                  nonce=record.nonce, origin=auth_origin())


def queue_reset(email):
    AuthMailJob.objects.create(kind='reset', email=email.strip().lower(), origin=auth_origin())


def verification_record(token, lock=False):
    try:
        data = signing.loads(token, salt=VERIFY_SALT, max_age=settings.EMAIL_VERIFICATION_TIMEOUT)
        if not isinstance(data, dict):
            return None
        records = EmailVerification.objects.select_related('owner')
        if lock:
            records = records.select_for_update()
        record = records.filter(owner_id=data.get('uid'), email=data.get('email'),
                                nonce=data.get('nonce'), verified_at__isnull=True, owner__is_active=True).first()
        if record and record.email == record.owner.email.strip().lower():
            return record
    except (signing.BadSignature, ValueError, TypeError, OverflowError):
        pass
    return None


def message_for(job):
    if job.kind == 'reset':
        user = get_user_model().objects.annotate(email_key=Lower(Trim('email'))).filter(
            email_key=job.email, is_active=True).first()
        if not user or not user.has_usable_password():
            return None
        path = reverse('password_reset_confirm', kwargs={
            'uidb64': urlsafe_base64_encode(force_bytes(user.pk)),
            'token': default_token_generator.make_token(user),
        })
        return ('Восстановление пароля — TenderSearch',
                f'Для аккаунта «{user.username}» запрошена смена пароля.\n\n'
                f'Откройте ссылку и задайте новый пароль:\n{job.origin}{path}\n\n'
                'Ссылка действует 1 час и перестаёт работать после смены пароля. '
                'Если вы не запрашивали восстановление, просто проигнорируйте письмо.\n\nTenderSearch')
    if job.kind == 'verify':
        record = EmailVerification.objects.filter(owner_id=job.owner_id, email=job.email,
            nonce=job.nonce, verified_at__isnull=True, owner__is_active=True).select_related('owner').first()
        if not record or record.email != record.owner.email.strip().lower():
            return None
        token = signing.dumps({'uid': record.owner_id, 'email': record.email, 'nonce': record.nonce}, salt=VERIFY_SALT)
        path = reverse('email_confirm', kwargs={'token': token})
        return ('Подтвердите email — TenderSearch',
                f'Подтвердите адрес электронной почты аккаунта «{record.owner.username}».\n\n'
                f'Откройте ссылку и нажмите «Подтвердить email»:\n{job.origin}{path}\n\n'
                'Ссылка действует 24 часа. Повторная отправка делает предыдущую ссылку недействительной. '
                'Если вы не создавали аккаунт, проигнорируйте письмо.\n\nTenderSearch')
    return None


def deliver_one():
    now = timezone.now()
    with transaction.atomic():
        job = AuthMailJob.objects.select_for_update(skip_locked=True).filter(
            completed_at__isnull=True, available_at__lte=now).order_by('pk').first()
        if job is None:
            return False
        timeout = settings.PASSWORD_RESET_TIMEOUT if job.kind == 'reset' else settings.EMAIL_VERIFICATION_TIMEOUT
        try:
            message = message_for(job) if now < job.created_at + timedelta(seconds=timeout) else None
            if message:
                sent = send_mail(*message, settings.DEFAULT_FROM_EMAIL, [job.email], fail_silently=False)
                if sent != 1:
                    raise RuntimeError('Mail backend did not accept message')
        except Exception as error:
            # Never persist/log SMTP error text, message bodies, tokens or credentials.
            job.attempts += 1
            job.last_error = type(error).__name__[:100]
            job.available_at = now + timedelta(seconds=min(600, 30 * 2**min(job.attempts, 5)))
            job.save(update_fields=['attempts', 'last_error', 'available_at'])
            return True
        job.completed_at, job.email, job.nonce, job.last_error = now, '', '', ''
        job.save(update_fields=['completed_at', 'email', 'nonce', 'last_error'])
    return True


def cleanup():
    now = timezone.now()
    AuthRateBucket.objects.filter(started_at__lt=now-timedelta(days=2)).delete()
    AuthMailJob.objects.filter(completed_at__lt=now-timedelta(days=7)).delete()
