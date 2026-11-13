from datetime import timedelta
from django import forms
from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods, require_POST

from .access import access_for, remaining_days
from .models import CompanySettings, TrialAccess, EmailVerification
from .auth_mail import queue_verification
from .auth_security import rate_limited
from .user_admin import CreateUserForm, can_manage_users


class RegistrationForm(CreateUserForm):
    class Meta(CreateUserForm.Meta):
        fields = ('username', 'email')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['email'].widget.attrs['autocomplete'] = 'email'
        self.fields['username'].widget.attrs['autocomplete'] = 'username'


@require_http_methods(['GET', 'HEAD'])
def landing(request):
    from .guides import GUIDES
    return render(request, 'public/landing.html', {'featured_guides': GUIDES[:3]})


@sensitive_post_parameters('password1', 'password2')
@require_http_methods(['GET', 'POST'])
@rate_limited('register')
def register(request):
    if request.user.is_authenticated:
        return redirect('dashboard')
    form = RegistrationForm(request.POST if request.method == 'POST' else None)
    if request.method == 'POST' and form.is_valid():
        try:
            with transaction.atomic():
                user = form.save()
                TrialAccess.objects.create(owner=user)
                EmailVerification.objects.create(owner=user, email=user.email, required=True)
                queue_verification(user)
                # Never copy the installation owner's company details to a new customer.
                CompanySettings.objects.create(owner=user, is_vat_payer=False, **{
                    field: '' for field in ('company_bin', 'company_name', 'address_city',
                    'address_street', 'address_house', 'address_office', 'legal_address',
                    'bank_account', 'bank_name', 'bank_bic', 'contact_phone')
                })
        except IntegrityError:
            form.add_conflict_error()
        else:
            login(request, user)
            messages.success(request, 'Аккаунт создан. Подтвердите email, чтобы начать работу. Ваши 14 бесплатных дней уже начались.')
            return redirect('email_notice')
    return render(request, 'public/register.html', {'form': form})


@login_required
@require_http_methods(['GET'])
def account_access(request):
    access = None if request.user.is_superuser else access_for(request.user)
    return render(request, 'public/access.html', {
        'access': access, 'days_left': remaining_days(access.expires_at) if access else None,
    })


class AccessDaysForm(forms.Form):
    days = forms.IntegerField(label='Осталось дней доступа', min_value=0, max_value=3650,
        help_text='Срок отсчитывается с момента сохранения. 0 — закрыть доступ сейчас.')


@login_required
@require_POST
def edit_access(request, user_id):
    if not can_manage_users(request.user):
        raise PermissionDenied
    account = get_object_or_404(get_user_model(), pk=user_id)
    if account.is_superuser:
        messages.error(request, 'У администраторов неограниченный доступ.')
        return redirect('administration_user_edit', user_id=user_id)
    form = AccessDaysForm(request.POST)
    if form.is_valid():
        TrialAccess.objects.update_or_create(owner=account, defaults={
            'expires_at': timezone.now() + timedelta(days=form.cleaned_data['days']),
        })
        messages.success(request, f'Срок доступа «{account.username}» изменён: {form.cleaned_data["days"]} дн.')
        return redirect('administration_users')
    from .user_admin import EditUserForm
    return render(request, 'core/user_edit.html', {'form': EditUserForm(instance=account),
        'account': account, 'access_form': form, 'account_access': access_for(account)}, status=400)
