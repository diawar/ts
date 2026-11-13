"""Durable personal queue; portal actions run only in the user's extension."""
import json
import uuid
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import JsonResponse
from django.http import Http404
from django.core.exceptions import PermissionDenied
from django.utils.dateparse import parse_datetime
from django.utils import timezone
from datetime import timedelta
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_POST
from .models import DraftQueueItem, UserLotWorkflow, CompanySettings, ApplicationPreparation, SearchProfile
from .preparation import save_portal_report, supports_preparation_export, PreparationForm
from .workflow_state import update_workflow


def expire_draft_queue(owner=None):
    items=DraftQueueItem.objects.filter(state__in=['queued','ready','running','waiting','paused','blocked'],lot__announcement__deadline__lte=timezone.now())
    if owner is not None:items=items.filter(owner=owner)
    for pk in list(items.values_list('pk',flat=True)):
        items.filter(pk=pk).update(state='cancelled',generation=uuid.uuid4(),message='Исключён из очереди: приём заявок завершён.',updated_at=timezone.now())
    # Queue cancellation and CRM stage are separate records. Repair even old
    # cancelled/completed queue entries, preserving preparation and portal evidence.
    closed=UserLotWorkflow.objects.filter(stage='preparing',lot__announcement__deadline__lte=timezone.now())
    if owner is not None:closed=closed.filter(owner=owner)
    for pk,owner_id in list(closed.values_list('pk','owner_id')):
        with transaction.atomic():
            list(SearchProfile.objects.select_for_update().filter(owner_id=owner_id).order_by('pk'))
            state=closed.select_for_update().filter(pk=pk).first()
            if not state:continue
            update_workflow(owner_id,state.lot_id,stage='interesting',expired_from_stage='',result_tracking=True)
            from .outcome_service import register_watch
            register_watch(owner_id,state.lot_id)


@login_required
@require_GET
def queue(request):
    expire_draft_queue(request.user)
    from .collection_views import collections
    response=collections(request, preparation_stage=True)
    response['Cache-Control']='no-store, private'
    return response


@login_required
@require_POST
def enqueue(request):
    from django.utils.http import url_has_allowed_host_and_scheme
    target=request.POST.get('return_to','')
    if not target or not url_has_allowed_host_and_scheme(target,allowed_hosts={request.get_host()},require_https=request.is_secure()):
        target='draft_queue'
    values=list(dict.fromkeys(request.POST.getlist('lots')))
    if not values or len(values)>20 or any(not value.isdecimal() for value in values):
        messages.error(request,'Выберите от 1 до 20 интересных лотов.')
        return redirect(target)
    count=0
    with transaction.atomic():
        list(SearchProfile.objects.select_for_update().filter(owner=request.user).order_by('pk'))
        workflows=UserLotWorkflow.objects.select_for_update().select_related('lot__announcement').filter(owner=request.user,lot_id__in=values,stage__in=('interesting','preparing'))
        for workflow in workflows:
            if not supports_preparation_export(workflow.lot) or (workflow.lot.announcement.deadline and workflow.lot.announcement.deadline<=timezone.now()):continue
            item,created=DraftQueueItem.objects.get_or_create(owner=request.user,lot=workflow.lot)
            if item.state=='cancelled':
                item.state='queued';item.progress_phase='';item.message='';item.generation=uuid.uuid4();item.save()
            update_workflow(request.user.pk,workflow.lot_id,stage='preparing')
            count+=1
    if count:messages.success(request,f'В очереди: {count}. Повторно выбранные лоты не дублируются.')
    if count<len(values):messages.warning(request,'Часть лотов не добавлена: нужен этап «Интересные лоты» и поддерживаемая площадка Госзакупа.')
    return redirect(target)


@login_required
@require_POST
def packet(request,item_id):
    item=get_object_or_404(DraftQueueItem.objects.select_related('lot__announcement'),owner=request.user,pk=item_id)
    if item.lot.announcement.deadline and item.lot.announcement.deadline<=timezone.now() and item.state!='complete':
        expire_draft_queue(request.user)
        return JsonResponse({'skipped':True,'state':'cancelled','message':'Приём заявок завершён. Лот исключён из очереди.'})
    if item.state in ('complete','cancelled'):
        return JsonResponse({'error':'Этот пункт очереди уже завершён или убран.'},status=400)
    saved=ApplicationPreparation.objects.filter(owner=request.user,lot=item.lot).first()
    if not saved or not PreparationForm(saved.data).is_valid():
        return JsonResponse({'error':'Сначала заполните и сохраните подготовку этого лота.'},status=400)
    from .draft_readiness import readiness
    from .lot_pricing import attach_lot_budgets
    company=CompanySettings.objects.filter(owner=request.user).first()
    attach_lot_budgets([item.lot])
    availability=readiness(item,company,saved)
    if not availability['can_start']:
        return JsonResponse({'error':availability['reason']},status=400)
    from .preparation import export_saved_preparation
    try:
        get_object_or_404(UserLotWorkflow,owner=request.user,lot_id=item.lot_id,stage__in=('interesting','preparing'))
        saved.lot=item.lot
        data=export_saved_preparation(saved,company)
    except (ValueError,Http404,PermissionDenied):
        return JsonResponse({'error':'Подготовка недоступна: проверьте БИН, этап и площадку.'},status=400)
    data['acceptance_deadline']=item.lot.announcement.deadline.isoformat() if item.lot.announcement.deadline else ''
    count=data.get('position_count')
    if type(count) is not int or count<1:
        return JsonResponse({'error':'Состав лота не подтверждён. Проверьте подготовку в карточке.'},status=400)
    if item.portal_check.get('state')=='requeued' and not data.get('existing_application_number'):
        data['reset_absent_application']=item.portal_check['number']
    response=JsonResponse({'packet':data,'queue_context':{'id':item.pk,'generation':str(item.generation)}})
    response['Cache-Control']='no-store, private'
    return response


@login_required
@require_POST
def progress(request,item_id):
    try:
        if len(request.body)>12000:raise ValueError()
        payload=json.loads(request.body)
        if not isinstance(payload,dict) or not isinstance(payload.get('snapshot'),dict):raise ValueError()
        snapshot=payload['snapshot']
        with transaction.atomic():
            company=CompanySettings.objects.select_for_update().get(owner=request.user)
            list(SearchProfile.objects.select_for_update().filter(owner=request.user).order_by('pk'))
            item=get_object_or_404(DraftQueueItem.objects.select_for_update().select_related('lot__announcement'),owner=request.user,pk=item_id)
            if item.state=='cancelled' and item.lot.announcement.deadline and item.lot.announcement.deadline<=timezone.now():
                return JsonResponse({'saved':True,'state':'cancelled','label':'Приём завершён'})
            if payload.get('generation')!=str(item.generation) or item.state=='cancelled':raise ValueError()
            if (snapshot.get('company_bin')!=company.company_bin or snapshot.get('lot_number')!=item.lot.number or snapshot.get('announcement_number')!=item.lot.announcement.number):raise ValueError()
            state=snapshot.get('status')
            if state not in ('ready','running','waiting','paused','blocked','complete'):raise ValueError()
            if item.state=='complete' and state!='complete':raise ValueError()
            has_draft=bool(snapshot.get('number') and snapshot.get('lotConfirmed') is True and snapshot.get('observed_at'))
            if state=='complete' and (not has_draft or snapshot.get('description_saved') is not True):raise ValueError()
            if has_draft:
                number=snapshot.get('number','')
                observed=parse_datetime(str(snapshot.get('observed_at','')))
                if not isinstance(number,str) or not observed or timezone.is_naive(observed) or observed>timezone.now()+timedelta(minutes=2):raise ValueError()
                report={'schema':'goszakup-status-v1','state':'draft','description_saved':state=='complete',
                    'company_bin':company.company_bin,'announcement_number':item.lot.announcement.number,'lot_number':item.lot.number,
                    'number':number,'url':'https://zakup.gov.kz/cabinet/purchase/my-applications/'+str(number),'observed_at':snapshot.get('observed_at')}
                saved=ApplicationPreparation.objects.filter(owner=request.user,lot=item.lot).first()
                if not(saved and saved.company_bin==company.company_bin and saved.portal_number==number and (state!='complete' or saved.portal_description_saved_at) and saved.portal_checked_at and saved.portal_checked_at>=observed):
                    # Preserve idempotency across repeated browser polls.
                    save_portal_report(request.user,item.lot_id,report,allow_create=True)
            from .draft_progress import snapshot_phase
            from django.template.loader import render_to_string
            item.progress_phase=snapshot_phase(snapshot,item.progress_phase)
            item.state=state;item.message=str(snapshot.get('message',''))[:500];item.save(update_fields=['state','message','progress_phase','updated_at'])
            workflow_stage=UserLotWorkflow.objects.filter(owner=request.user,lot_id=item.lot_id).values_list('stage',flat=True).first()
            return JsonResponse({'saved':True,'state':item.state,'workflow_stage':workflow_stage,'label':item.get_state_display(),'steps_html':render_to_string('core/_draft_progress.html',{'item':item})})
    except (ValueError,TypeError,KeyError,CompanySettings.DoesNotExist):
        return JsonResponse({'error':'Прогресс не сохранён: проверьте аккаунт, БИН и актуальность очереди.'},status=400)


@login_required
@require_POST
def cancel(request,item_id):
    with transaction.atomic():
        item=get_object_or_404(DraftQueueItem.objects.select_for_update(),owner=request.user,pk=item_id)
        if item.state in ('running','waiting'):
            messages.error(request,'Сначала остановите заполнение в расширении и дождитесь обновления очереди.')
        else:
            item.state='cancelled';item.save(update_fields=['state','updated_at'])
            if UserLotWorkflow.objects.filter(owner=request.user,lot_id=item.lot_id,stage='preparing').exists():
                update_workflow(request.user.pk,item.lot_id,stage='interesting')
    return redirect('draft_queue')


@login_required
@require_POST
def check_portal(request,item_id):
    item=get_object_or_404(DraftQueueItem.objects.select_related('lot__announcement'),owner=request.user,pk=item_id)
    company=CompanySettings.objects.filter(owner=request.user).first()
    saved=ApplicationPreparation.objects.filter(owner=request.user,lot=item.lot).first()
    if item.state in ('running','waiting','cancelled') or not company or not saved or saved.company_bin!=company.company_bin or not saved.portal_number:
        return JsonResponse({'error':'Проверка недоступна: остановите очередь и проверьте БИН и сохранённый номер заявки.'},status=400)
    target={'company_bin':company.company_bin,'announcement_number':item.lot.announcement.number,'lot_number':item.lot.number,'number':saved.portal_number}
    if request.content_type!='application/json':
        response=JsonResponse({'target':target,'generation':str(item.generation)})
        response['Cache-Control']='no-store, private'
        return response
    try:
        if len(request.body)>4000:raise ValueError()
        data=json.loads(request.body);result=data['result']
        if data['generation']!=str(item.generation) or any(result.get(k)!=v for k,v in target.items()):raise ValueError()
        state=result.get('check_state')
        labels={'found':'Заявка найдена','absent':'Черновик отсутствует в результатах поиска','unknown':'Не удалось проверить'}
        observed=parse_datetime(result.get('observed_at',''))
        if state not in labels or not observed or timezone.is_naive(observed) or abs((timezone.now()-observed).total_seconds())>300:raise ValueError()
        item.portal_check={'state':state,'label':labels[state],'number':saved.portal_number,'checked_at':observed.isoformat(),'detail':str(result.get('message',''))[:300]}
        item.save(update_fields=['portal_check','updated_at'])
        return JsonResponse({'saved':True,'label':labels[state]})
    except (ValueError,KeyError,TypeError,AttributeError):
        return JsonResponse({'error':'Результат проверки не соответствует лоту или устарел.'},status=400)


@login_required
@require_POST
def restart(request,item_id):
    with transaction.atomic():
        company=CompanySettings.objects.select_for_update().filter(owner=request.user).first()
        item=get_object_or_404(DraftQueueItem.objects.select_for_update(),owner=request.user,pk=item_id)
        saved=ApplicationPreparation.objects.select_for_update().filter(owner=request.user,lot=item.lot).first()
        check=item.portal_check
        observed=parse_datetime(str(check.get('checked_at','')))
        if (request.POST.get('generation')!=str(item.generation) or item.state in ('running','waiting','cancelled') or (item.lot.announcement.deadline and item.lot.announcement.deadline<=timezone.now()) or not saved or not company
            or saved.company_bin!=company.company_bin or saved.portal_state!='draft' or check.get('state')!='absent'
            or not saved.portal_number or check.get('number')!=saved.portal_number or not observed or timezone.is_naive(observed)
            or not timedelta(0)<=timezone.now()-observed<=timedelta(minutes=30)):
            messages.error(request,'Сначала выполните свежую проверку отсутствия черновика на Госзакупе. Заполнение должно быть остановлено.')
            return redirect('draft_queue')
        number=saved.portal_number
        saved.portal_state='unconfirmed';saved.portal_number='';saved.portal_url='';saved.portal_checked_at=None
        saved.portal_confirmed_at=None;saved.portal_description_saved_at=None;saved.portal_sync_error=''
        saved.save()
        if UserLotWorkflow.objects.filter(owner=request.user,lot_id=item.lot_id,stage='awaiting_results').exists():
            update_workflow(request.user.pk,item.lot_id,stage='preparing',result_tracking=False)
            from .fulfillment import pause_outcome
            pause_outcome(request.user.pk,item.lot_id)
        item.portal_check={**check,'state':'requeued','label':'Возвращён в очередь после проверки отсутствия','number':number}
        item.generation=uuid.uuid4();item.state='queued';item.progress_phase='';item.message='Подготовка сохранена. Можно запускать очередь заново.';item.save()
        messages.success(request,'Лот возвращён в очередь. Данные товара сохранены; нажмите «Запустить очередь».')
    return redirect('draft_queue')
