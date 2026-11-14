from django.http import Http404
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Max, Subquery, F, DateTimeField, Value
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast, NullIf
from urllib.parse import urlencode
from django.shortcuts import get_object_or_404, render, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from .models import SearchProfile, CollectionLot, Favorite, AnnouncementWatch, TelegramSubscription, TelegramDelivery, UserLotNote, LotOutcomeWatch, UserLotPrice
from .collections_service import enqueue_collection
from .lot_state import visible_collection_lots

STEPS = [('all', 'Неразобранные лоты'), ('preparing', 'В работе'), ('awaiting_results', 'Ожидаем итоги'), ('applied', 'Подали заявку'),
         ('contract', 'Заключение договора'), ('delivery', 'Поставка'), ('payment', 'Оплата'),
         ('won', 'Выиграли'), ('lost', 'Проиграли'), ('not_participated', 'Не участвовали'), ('closed', 'Закупка закрыта'), ('archived', 'Архив')]


@login_required
def collections(request, preparation_stage=False):
    from .pagination import page_settings
    pagination=page_settings(request,'workspace')
    profiles = list(SearchProfile.objects.filter(owner=request.user).order_by('created_at', 'pk'))
    selected = request.GET.get('profile')
    aggregate = not selected or selected == 'all'
    if not aggregate and not selected.isdecimal():
        raise Http404
    profile = None if aggregate else get_object_or_404(SearchProfile, pk=selected, owner=request.user)
    if profile and profile.filters.get('manual'):
        params = request.GET.copy()
        params['profile'] = 'all'
        return redirect(request.path + '?' + params.urlencode())
    stage = request.GET.get('stage','preparing') if preparation_stage else request.GET.get('stage', 'all')
    if stage == 'interesting':
        params = request.GET.copy()
        params['stage'] = 'preparing'
        return redirect(reverse('draft_queue') + '?' + params.urlencode())
    if preparation_stage and stage not in ('preparing','awaiting_results'):stage='preparing'
    if stage in ('preparing','awaiting_results') and not preparation_stage:
        params=request.GET.copy()
        params['profile']=selected or 'all'
        if stage=='awaiting_results':params['stage']=stage
        return redirect(reverse('draft_queue')+'?'+urlencode(params))
    if stage not in dict(STEPS):
        stage = 'all'
    selected_sort = request.GET.get('sort', '')
    sort_fields = {'amount': 'lot__amount', 'start': 'offer_start_sort', 'end': 'lot__announcement__deadline'}
    allowed_sorts = {prefix+key for prefix in ('', '-') for key in sort_fields}
    if selected_sort not in allowed_sorts:
        selected_sort = ''
    context = {**pagination,'collection_sort': selected_sort, 'profiles': profiles,
               'collection_tabs': [saved for saved in profiles if not saved.filters.get('manual')],
               'active_collection': profile, 'aggregate_collection': aggregate,
               'collection_key': 'all' if aggregate else str(profile.pk), 'stage': stage,
               'stage_choices': CollectionLot.STAGES, 'outcome_choices': CollectionLot.OUTCOMES,
               'telegram_connected': TelegramSubscription.objects.filter(owner=request.user, enabled=True, chat_id__isnull=False).exists()}
    if profiles:
        records = visible_collection_lots(request.user)
        if profile:
            records = records.filter(profile=profile)
        all_records = records
        counters = {key: set() for key, label in STEPS}
        visible_ids = set()
        for identifier, current_stage in records.values_list('lot_id', 'stage'):
            if current_stage != 'archived':
                visible_ids.add(identifier)
            if current_stage == 'new':
                counters['all'].add(identifier)
            if current_stage in counters:
                counters[current_stage].add(identifier)
        if aggregate:
            request._workspace_lot_count = len(visible_ids)
        context.update(steps=[{'key': key, 'label': label, 'count': len(counters[key])} for key, label in STEPS],
            collection_busy=any(p.collection_lease_until and p.collection_lease_until > timezone.now() for p in ([profile] if profile else profiles)))
        if stage not in ('preparing','awaiting_results'):
            records = records.filter(stage='new' if stage == 'all' else stage)
            if aggregate:
                representatives = records.order_by().values('lot_id').annotate(identifier=Max('pk')).values('identifier')
                records = records.filter(pk__in=Subquery(representatives))
            order = ['-last_seen_at', '-pk']
            if selected_sort:
                key = selected_sort.lstrip('-')
                if key == 'start':
                    records = records.annotate(offer_start_sort=Cast(NullIf(KeyTextTransform('offer_start_date', 'lot__raw'), Value('')), DateTimeField()))
                field = F(sort_fields[key])
                order.insert(0, field.desc(nulls_last=True) if selected_sort.startswith('-') else field.asc(nulls_last=True))
            key = selected_sort.lstrip('-')
            headers = {}
            for name, label in [('amount', 'Сумма, ₸'), ('start', 'Начало'), ('end', 'Окончание')]:
                active = key == name
                descending = selected_sort.startswith('-')
                target = ('-' if active and not descending else '') + name
                headers[name] = {'label': label, 'url': '?' + urlencode({'profile': context['collection_key'], 'stage': stage, 'sort': target,'per_page':pagination['per_page']}),
                    'arrow': ('↓' if descending else '↑') if active else '↕', 'aria': ('descending' if descending else 'ascending') if active else 'none'}
            context['sort_headers'] = headers
            page = Paginator(records.select_related('lot__announcement', 'lot__customer').order_by(*order), pagination['per_page']).get_page(request.GET.get('page'))
            from .views import result_context
            result_context(request, [record.lot for record in page])
            notes = dict(UserLotNote.objects.filter(owner=request.user,
                lot_id__in=[record.lot_id for record in page]).values_list('lot_id', 'text'))
            workflows = {}
            if aggregate:
                for item in all_records.filter(lot_id__in=[record.lot_id for record in page]).select_related('profile').order_by('profile__created_at', 'pk'):
                    workflows.setdefault(item.lot_id, []).append(item)
            for record in page:
                record.result_watch = record.lot.personal_outcome_watch
                record.note_text = notes.get(record.lot_id, '')
                record.workflows = workflows.get(record.lot_id, [record])
                if aggregate:
                    record.terms = list(dict.fromkeys(term for item in record.workflows for term in item.terms))
            context['page'] = page
    if stage in ('preparing','awaiting_results'):
        from .models import DraftQueueItem, Lot
        from .lot_pricing import attach_lot_budgets
        ids=visible_collection_lots(request.user).filter(stage=stage)
        if profile: ids=ids.filter(profile=profile)
        lots = list(Lot.objects.filter(pk__in=ids.values('lot_id')).select_related('announcement', 'customer').order_by('announcement__deadline', 'pk'))
        queued = {item.lot_id: item for item in DraftQueueItem.objects.filter(owner=request.user, lot_id__in=[lot.pk for lot in lots])}
        membership_ids = {}
        for lot_id, record_id in ids.order_by('pk').values_list('lot_id', 'pk'):
            membership_ids.setdefault(lot_id, record_id)
        # Selection does not create an executable task. Show unqueued lots too.
        if selected_sort:
            sort_key = selected_sort.lstrip('-')
            field = {'amount': 'amount', 'start': 'offer_start_sort', 'end': 'announcement__deadline'}[sort_key]
            if sort_key == 'start':
                lots_query = Lot.objects.filter(pk__in=ids.values('lot_id')).annotate(offer_start_sort=Cast(NullIf(KeyTextTransform('offer_start_date', 'raw'), Value('')), DateTimeField()))
            else:
                lots_query = Lot.objects.filter(pk__in=ids.values('lot_id'))
            ordering = F(field).desc(nulls_last=True) if selected_sort.startswith('-') else F(field).asc(nulls_last=True)
            lots = list(lots_query.select_related('announcement', 'customer').order_by(ordering, 'pk'))
        from .lot_state import apply_personal_controls
        apply_personal_controls(request.user, lots)
        context['sort_headers'] = {}
        for name, label in [('amount', 'Сумма, ₸'), ('start', 'Начало'), ('end', 'Окончание')]:
            active = selected_sort.lstrip('-') == name
            descending = selected_sort.startswith('-')
            target = ('-' if active and not descending else '') + name
            context['sort_headers'][name] = {'label': label, 'url': '?' + urlencode({'profile': context['collection_key'], 'stage': stage, 'sort': target, 'per_page': pagination['per_page']}),
                'arrow': ('↓' if descending else '↑') if active else '↕', 'aria': ('descending' if descending else 'ascending') if active else 'none'}
        items = []
        from tender_search.client import utc_datetime, SchemaError
        for lot in lots:
            item = queued.get(lot.pk) or DraftQueueItem(owner=request.user, lot=lot)
            item.lot = lot
            try:
                item.offer_start = utc_datetime((lot.raw or {}).get('offer_start_date'))
            except (ValueError, TypeError, SchemaError):
                item.offer_start = None
            item.collection_item_id = membership_ids[lot.pk]
            items.append(item)
        attach_lot_budgets([item.lot for item in items])
        from .draft_readiness import readiness
        from .models import CompanySettings, ApplicationPreparation, UserLotWorkflow
        company=CompanySettings.objects.filter(owner=request.user).first()
        lot_ids=[item.lot_id for item in items]
        prepared={saved.lot_id:saved for saved in ApplicationPreparation.objects.filter(owner=request.user,lot_id__in=lot_ids)}
        workflows={workflow.lot_id:workflow for workflow in UserLotWorkflow.objects.filter(owner=request.user,lot_id__in=lot_ids)}
        for item in items:
            saved=prepared.get(item.lot_id)
            item.lot.preparation=saved if saved and company and saved.company_bin==company.company_bin else None
            item.lot.workflow=workflows.get(item.lot_id)
            item.availability=readiness(item,company,saved)
        context['queue_can_run']=any(item.pk and item.availability['can_start'] for item in items)
        context['items']=items
    return render(request, 'core/collections.html', context)


@login_required
@require_POST
def tracking(request, profile_id):
    error = ''
    with transaction.atomic():
        profile = get_object_or_404(SearchProfile.objects.select_for_update(), pk=profile_id, owner=request.user)
        try:
            hours = int(request.POST.get('hours', profile.collection_hours))
            if not 1 <= hours <= 12:
                raise ValueError()
        except (TypeError, ValueError):
            error = 'Интервал должен быть целым числом от 1 до 12 часов.'
        if not error:
            if request.POST.get('action') == 'toggle':
                profile.collection_enabled = not profile.collection_enabled
                profile.enabled = profile.collection_enabled
                profile.next_monitor_at = None
                if not profile.enabled:
                    TelegramDelivery.objects.filter(profile=profile, state='pending').update(state='cancelled')
            profile.interval_minutes = hours*60
            profile.next_monitor_at = None
            profile.collection_revision += 1
            profile.collection_lease = ''
            profile.collection_lease_until = None
            profile.collection_next_at = None
            profile.collection_error = ''
            profile.save(update_fields=['collection_enabled', 'interval_minutes', 'collection_revision',
                'collection_lease', 'collection_lease_until', 'collection_next_at', 'collection_error',
                'enabled', 'next_monitor_at', 'updated_at'])
            if profile.collection_enabled:
                transaction.on_commit(lambda: enqueue_collection(profile.pk))
    if request.headers.get('HX-Request'):
        missing = profile.enabled and not TelegramSubscription.objects.filter(owner=request.user, enabled=True, chat_id__isnull=False).exists()
        return render(request, 'core/_profile_tracking.html', {'profile': profile, 'tracking_error': error, 'telegram_missing': missing})
    return redirect(f"{reverse('workspace')}?profile={profile.pk}")


@login_required
@require_POST
def refresh(request, profile_id):
    profile = get_object_or_404(SearchProfile, pk=profile_id, owner=request.user)
    enqueue_collection(profile.pk, force=True)
    return redirect(f"{reverse('workspace')}?profile={profile.pk}")


@login_required
@require_POST
def move(request, item_id):
    from django.contrib import messages
    with transaction.atomic():
        candidate=get_object_or_404(CollectionLot,pk=item_id,profile__owner=request.user)
        list(SearchProfile.objects.select_for_update().filter(owner=request.user).order_by('pk'))
        item=get_object_or_404(CollectionLot.objects.select_for_update(),pk=item_id)
        from .workflow_state import update_workflow
        target_stage = request.POST.get('stage')
        if target_stage in ('interesting', 'preparing') and item.stage in ('new', 'not_participated'):
            destination = 'not_participated' if item.stage == 'not_participated' else 'all'
            deadline = item.lot.announcement.deadline
            update_workflow(request.user.pk, item.lot_id, stage='preparing', outcome='',
                result_tracking=False, expired_from_stage='',
                keep_after_deadline=bool(deadline and deadline <= timezone.now()))
        elif target_stage == 'not_participated' and item.stage in ('new', 'preparing'):
            from .workflow_state import remove_from_work
            destination = 'preparing' if item.stage == 'preparing' else 'all'
            if not remove_from_work(request.user.pk, item.lot_id):
                messages.error(request, 'Сначала остановите заполнение черновика и дождитесь подтверждения остановки.')
        else:
            messages.error(request,'Этап меняется автоматически по подтверждённым данным. Для подготовки используйте «Подготовить заявку».')
            destination='all'
    from django.utils.http import url_has_allowed_host_and_scheme
    from urllib.parse import urlsplit
    return_to=request.POST.get('return_to','')
    if (return_to and url_has_allowed_host_and_scheme(return_to,allowed_hosts={request.get_host()},require_https=request.is_secure())
            and urlsplit(return_to).path == reverse('workspace')):
        return redirect(return_to)
    target='all' if request.POST.get('return_scope')=='all' else item.profile_id
    selected_sort=request.POST.get('return_sort','')
    suffix='&sort='+selected_sort if selected_sort in {prefix+key for prefix in ('','-') for key in ('amount','start','end')} else ''
    return redirect(f"{reverse('workspace')}?profile={target}&stage={destination}{suffix}")


@login_required
@require_POST
def fulfillment_action(request, lot_id):
    from django.contrib import messages
    from django.utils.http import url_has_allowed_host_and_scheme
    from .fulfillment import advance
    if not CollectionLot.objects.filter(profile__owner=request.user,lot_id=lot_id).exists():
        raise Http404
    try:
        advance(request.user.pk,lot_id,request.POST.get('action',''))
    except ValueError as exc:
        messages.error(request,str(exc))
    target=request.POST.get('next','')
    if target and url_has_allowed_host_and_scheme(target,allowed_hosts={request.get_host()},require_https=request.is_secure()):
        return redirect(target)
    return redirect('workspace')
