import uuid

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView, PasswordResetConfirmView
from django.db import transaction
from django.shortcuts import redirect, render
from django.urls import reverse_lazy
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods, require_POST

from .auth_mail import queue_reset, queue_verification, verification_record
from .auth_security import rate_limited
from .models import EmailVerification


@method_decorator(rate_limited('login'), name='post')
class ProtectedLoginView(LoginView):
    template_name = 'registration/login.html'


class ResetRequestForm(forms.Form):
    email = forms.EmailField(label='Email', max_length=254,
        widget=forms.EmailInput(attrs={'autocomplete': 'email', 'autofocus': True}))


@sensitive_post_parameters('email')
@require_http_methods(['GET', 'POST'])
@rate_limited('reset')
def password_reset(request):
    form = ResetRequestForm(request.POST if request.method == 'POST' else None)
    if request.method == 'POST' and form.is_valid():
        queue_reset(form.cleaned_data['email'])
        return redirect('password_reset_done')
    return render(request, 'public/auth_form.html', {'form': form,
        'form_title': 'Восстановление пароля', 'button_text': 'Отправить ссылку',
        'form_intro': 'Введите email, указанный при регистрации. Мы отправим ссылку для смены пароля.'})


@method_decorator(rate_limited('reset-confirm'), name='post')
class ProtectedPasswordResetConfirmView(PasswordResetConfirmView):
    template_name = 'public/auth_form.html'
    success_url = reverse_lazy('password_reset_complete')
    extra_context = {'form_title': 'Новый пароль', 'button_text': 'Сохранить пароль',
                     'form_intro': 'Задайте новый пароль для вашего аккаунта.', 'reset_confirmation': True}


@login_required
@require_http_methods(['GET'])
def email_notice(request):
    record = EmailVerification.objects.filter(owner=request.user).first()
    confirmed = bool(record and record.verified_at and record.email == request.user.email.strip().lower())
    return render(request, 'public/email_notice.html', {'confirmed': confirmed})


@login_required
@require_POST
@rate_limited('verify-resend')
def email_resend(request):
    record = EmailVerification.objects.filter(owner=request.user).first()
    if record and record.verified_at and record.email == request.user.email.strip().lower():
        messages.info(request, 'Email уже подтверждён.')
    elif request.user.email:
        queue_verification(request.user)
        messages.success(request, 'Письмо поставлено в очередь отправки. Проверьте почту и папку «Спам».')
    else:
        messages.error(request, 'В аккаунте не указан email. Обратитесь к администратору.')
    return redirect('email_notice')


@require_http_methods(['GET', 'POST'])
@rate_limited('verify-confirm')
def email_confirm(request, token):
    # Mail scanners and previews must not consume a verification link.
    confirmed = False
    with transaction.atomic():
        record = verification_record(token, lock=request.method == 'POST')
        valid = record is not None
        if valid and request.method == 'POST':
            record.verified_at = timezone.now()
            record.nonce = uuid.uuid4().hex
            record.save(update_fields=['verified_at', 'nonce'])
            confirmed = True
    return render(request, 'public/email_confirm.html', {'valid': valid, 'confirmed': confirmed})
