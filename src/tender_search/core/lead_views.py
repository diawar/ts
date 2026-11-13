import csv
from datetime import timedelta

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Count, Prefetch, Q
from django.db.models.functions import Lower
from django.http import StreamingHttpResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from .lead_service import set_collection_enabled
from .models import LeadCollectionState, LeadCompany, LeadEmail


def require_staff(request):
    if not settings.LEAD_COLLECTION_AVAILABLE:
        raise Http404
    from .user_admin import can_manage_users
    if not can_manage_users(request.user):
        raise PermissionDenied


def collection_context():
    state, _ = LeadCollectionState.objects.get_or_create(pk=1)
    companies = LeadCompany.objects.filter(emails__current=True).exclude(status='excluded').aggregate(
        with_email=Count('pk', distinct=True))
    emails = LeadEmail.objects.filter(current=True).exclude(company__status='excluded').annotate(
        email_key=Lower('email')).values('email_key').distinct().count()
    return {'collection': state, 'lead_totals': companies, 'email_count': emails,
            'collector_recent': bool(state.heartbeat_at and state.heartbeat_at >= timezone.now()-timedelta(minutes=3))}


def filtered_companies(request):
    companies = LeadCompany.objects.filter(emails__current=True).distinct()
    query = request.GET.get('q', '').strip()[:200]
    if query:
        companies = companies.filter(Q(name__icontains=query) | Q(identifier__icontains=query) |
                                     Q(emails__email__icontains=query)).distinct()
    status = request.GET.get('status', '')
    if status in dict(LeadCompany.STATUS_CHOICES):
        companies = companies.filter(status=status)
    else:
        companies = companies.exclude(status='excluded')
    return companies.order_by('-created_at', '-pk'), query


@login_required
def leads(request):
    require_staff(request)
    if request.method == 'POST':
        action = request.POST.get('action')
        if action in {'start', 'pause'}:
            set_collection_enabled(action == 'start')
            messages.success(request, 'Фоновый сбор включён.' if action == 'start' else 'Фоновый сбор приостановлен.')
        return redirect('administration_leads')
    companies, query = filtered_companies(request)
    companies = companies.prefetch_related(Prefetch('emails', queryset=LeadEmail.objects.filter(current=True).order_by(
        '-verified_at', 'email'), to_attr='active_emails'))
    page = Paginator(companies, 25).get_page(request.GET.get('page'))
    params = request.GET.copy()
    params.pop('page', None)
    params.pop('contact', None)
    context = collection_context()
    context.update(page=page, query=query, status_choices=LeadCompany.STATUS_CHOICES,
        page_query=params.urlencode())
    return render(request, 'core/leads.html', context)


@login_required
def collection_status(request):
    require_staff(request)
    return render(request, 'core/_lead_collection_status.html', collection_context())


class LeadEditForm(forms.ModelForm):
    notes = forms.CharField(label='Заметки', required=False, max_length=4000, widget=forms.Textarea(attrs={'rows': 4}))

    class Meta:
        model = LeadCompany
        fields = ['status', 'notes']
        labels = {'status': 'Состояние клиента', 'notes': 'Заметки'}


@login_required
def lead_detail(request, company_id):
    require_staff(request)
    company = get_object_or_404(LeadCompany, pk=company_id)
    form = LeadEditForm(request.POST or None, instance=company)
    if request.method == 'POST':
        if request.POST.get('action') == 'refresh':
            if company.status == 'excluded':
                messages.info(request, 'Для перепроверки контактов сначала измените состояние «Не связываться».')
                return redirect('administration_lead_detail', company_id=company.pk)
            LeadCompany.objects.filter(pk=company.pk).update(next_contact_check_at=timezone.now(),
                contact_state='pending', contact_error='', updated_at=timezone.now())
            messages.success(request, 'Контакты будут перепроверены фоновым сборщиком.')
            return redirect('administration_lead_detail', company_id=company.pk)
        if form.is_valid():
            form.save()
            messages.success(request, 'Карточка клиента сохранена.')
            return redirect('administration_lead_detail', company_id=company.pk)
    page = Paginator(company.participations.select_related('procurement').order_by(
        '-procurement__results_published_at'), 25).get_page(request.GET.get('page'))
    return render(request, 'core/lead_detail.html', {'company': company, 'form': form,
        'emails': company.emails.order_by('-current', '-verified_at'), 'page': page})


def csv_cell(value):
    value = str(value or '')
    return "'"+value if value.lstrip().startswith(('=', '+', '-', '@')) else value


class Echo:
    def write(self, value):
        return value


@login_required
def export_leads(request):
    require_staff(request)
    companies, _ = filtered_companies(request)
    companies = companies.exclude(status='excluded').filter(emails__current=True).distinct().prefetch_related(
        Prefetch('emails', queryset=LeadEmail.objects.filter(current=True).order_by('email'), to_attr='active_emails'))
    writer = csv.writer(Echo(), delimiter=';')

    def rows():
        yield '\ufeff'+writer.writerow(['Компания', 'E-mail'])
        seen = set()
        for company in companies.iterator(chunk_size=200):
            for contact in company.active_emails:
                key = contact.email.casefold()
                if key in seen:
                    continue
                seen.add(key)
                yield writer.writerow([csv_cell(company.name), csv_cell(contact.email)])
    response = StreamingHttpResponse(rows(), content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = 'attachment; filename="tendersearch-clients.csv"'
    return response
