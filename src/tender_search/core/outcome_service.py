"""Checks start only for explicitly applied collection lots and stay independent of Telegram polling."""
from .access import eligible_user_ids
from datetime import timedelta
from hashlib import sha256
from html import escape
import secrets
from django.db import transaction
from django.db.models import Q, F, Exists, OuterRef
from django.utils import timezone
from tender_search.client import SourceError
from .models import CompanySettings, CollectionLot, LotOutcomeWatch, OutcomeDelivery, SearchProfile, TelegramSubscription, UserLotPrice
from .source import source_client
from .outcome_source import OutcomePortal, Outcome, ProtocolFormatError
from .telegram_service import telegram_api, TelegramError

TRACKED_STAGES = ('interesting','preparing','awaiting_results','applied','lost','contract','delivery','payment')


def enroll_participation_checks():
    from .models import UserLotWorkflow
    from .workflow_state import update_workflow
    now=timezone.now()
    reopened=UserLotWorkflow.objects.filter(owner_id__in=eligible_user_ids(),stage__in=('interesting','preparing','awaiting_results'),result_tracking=True,
        lot__announcement__deadline__gt=now)
    for pk in list(reopened.values_list('pk',flat=True)[:200]):
        with transaction.atomic():
            state=UserLotWorkflow.objects.filter(pk=pk).first()
            if not state:continue
            list(SearchProfile.objects.select_for_update().filter(owner_id=state.owner_id).order_by('pk'))
            state=reopened.select_for_update().filter(pk=pk).first()
            if not state:continue
            update_workflow(state.owner_id,state.lot_id,result_tracking=False)
            from .fulfillment import pause_outcome
            pause_outcome(state.owner_id,state.lot_id)
    candidates=UserLotWorkflow.objects.filter(owner_id__in=eligible_user_ids()).filter(
        Q(stage='interesting',result_tracking=False,lot__announcement__deadline__lte=now) |
        Q(stage='preparing',lot__announcement__deadline__lte=now) |
        Q(stage='awaiting_results',result_tracking=False,lot__announcement__deadline__lte=now) |
        Q(stage='not_participated',expired_from_stage='interesting',lot__announcement__deadline__lte=now))
    for pk in list(candidates.values_list('pk',flat=True)[:200]):
        with transaction.atomic():
            state=UserLotWorkflow.objects.filter(pk=pk).first()
            if not state:continue
            list(SearchProfile.objects.select_for_update().filter(owner_id=state.owner_id).order_by('pk'))
            state=candidates.select_for_update().filter(pk=pk).first()
            if not state:continue
            update_workflow(state.owner_id,state.lot_id,stage='awaiting_results' if state.stage=='awaiting_results' else 'interesting',expired_from_stage='',result_tracking=True)
            register_watch(state.owner_id,state.lot_id)


def company_bin_for(owner_id):
    return CompanySettings.objects.filter(owner_id=owner_id).values_list('company_bin',flat=True).first() or ''


def tracked_items(owner_id, lot_id):
    return CollectionLot.objects.filter(profile__owner_id=owner_id,lot_id=lot_id,result_tracking=True,stage__in=TRACKED_STAGES)


def register_watch(owner_id, lot_id):
    company_bin=company_bin_for(owner_id)
    with transaction.atomic():
        watch,created=LotOutcomeWatch.objects.select_for_update().get_or_create(owner_id=owner_id,lot_id=lot_id,
            defaults={'company_bin':company_bin,'state':'pending' if company_bin else 'no_bin'})
        if not created:
            reset=not watch.enabled or watch.company_bin!=company_bin
            watch.enabled=True;watch.next_check_at=None
            if reset:
                watch.company_bin=company_bin;watch.generation=secrets.token_hex(16)
                watch.state='pending' if company_bin else 'no_bin';watch.winner_bin='';watch.winner_name=''
                watch.protocol_url='';watch.fingerprint='';watch.revision=0;watch.lease='';watch.lease_until=None;watch.error=''
                OutcomeDelivery.objects.filter(watch=watch,state='pending').update(state='cancelled')
            watch.save()
    return watch


def reset_company(owner_id, new_bin):
    with transaction.atomic():
        company,_=CompanySettings.objects.select_for_update().get_or_create(owner_id=owner_id)
        if company.company_bin==new_bin:return
        company.company_bin=new_bin;company.save(update_fields=['company_bin','updated_at'])
        list(SearchProfile.objects.select_for_update().filter(owner_id=owner_id).order_by('pk'))
        from .contract_service import invalidate
        invalidate(owner_id)
        from .workflow_state import update_workflow
        for lot_id in CollectionLot.objects.filter(profile__owner_id=owner_id,result_tracking=True,
                stage__in=['won','lost']).values_list('lot_id',flat=True).distinct():
            update_workflow(owner_id, lot_id, stage='applied', outcome='')
        for lot_id in CollectionLot.objects.filter(profile__owner_id=owner_id,result_tracking=True,
                stage__in=['contract','delivery','payment']).values_list('lot_id',flat=True).distinct():
            update_workflow(owner_id, lot_id, outcome='')
        watches=LotOutcomeWatch.objects.select_for_update().filter(owner_id=owner_id)
        for watch in watches:
            watch.company_bin=new_bin;watch.generation=secrets.token_hex(16)
            watch.state='pending' if new_bin else 'no_bin';watch.winner_bin='';watch.winner_name=''
            watch.protocol_url='';watch.fingerprint='';watch.revision=0;watch.lease='';watch.lease_until=None
            watch.next_check_at=None;watch.error='';watch.save()
            OutcomeDelivery.objects.filter(watch=watch,state='pending').update(state='cancelled')


def schedule_outcomes():
    from .tasks import check_lot_outcome
    now=timezone.now()
    enroll_participation_checks()
    candidates=CollectionLot.objects.filter(result_tracking=True,stage__in=TRACKED_STAGES,profile__owner_id__in=eligible_user_ids())
    candidates=candidates.annotate(has_watch=Exists(LotOutcomeWatch.objects.filter(owner_id=OuterRef('profile__owner_id'),lot_id=OuterRef('lot_id')))).filter(has_watch=False)
    for owner_id,lot_id in candidates.values_list('profile__owner_id','lot_id').distinct()[:100]:
        register_watch(owner_id,lot_id)
    watches=LotOutcomeWatch.objects.filter(enabled=True,owner_id__in=eligible_user_ids()).filter(
        Q(next_check_at__isnull=True)|Q(next_check_at__lte=now)).filter(Q(lease_until__isnull=True)|Q(lease_until__lte=now))
    for watch in watches.order_by('next_check_at','pk')[:30]:
        if not tracked_items(watch.owner_id,watch.lot_id).exists():
            LotOutcomeWatch.objects.filter(pk=watch.pk).update(enabled=False,lease='',lease_until=None)
            OutcomeDelivery.objects.filter(watch=watch,state='pending').update(state='cancelled');continue
        lease=secrets.token_hex(16)
        claimed=LotOutcomeWatch.objects.filter(pk=watch.pk,enabled=True).filter(Q(lease_until__isnull=True)|Q(lease_until__lte=now)).update(lease=lease,lease_until=now+timedelta(minutes=10))
        if claimed:
            try:check_lot_outcome.delay(watch.pk,lease)
            except Exception:
                LotOutcomeWatch.objects.filter(pk=watch.pk,lease=lease).update(lease='',lease_until=None,
                    next_check_at=now+timedelta(minutes=5),error='Очередь проверки итогов недоступна.')


def supported_source(lot):
    if lot.announcement.source!='zakup' or not lot.announcement.external_id or not lot.number or not lot.announcement.number:return False
    ids=set()
    for raw in (lot.raw or {},lot.announcement.raw or {}):
        system=raw.get('system')
        for value in (raw.get('system_id'),system.get('id') if isinstance(system,dict) else system):
            if value is not None and value!='':ids.add(str(value))
    if ids == {'1'}:
        return True
    # Observed unified API: legacy EGZ lots (system 1) have EPZ (4) on
    # their announcement. The public protocol still has to bind both exact numbers.
    raw = lot.raw or {}
    lot_system = raw.get('system_id') or (raw.get('system') or {}).get('id')
    return (ids == {'1', '4'} and str(lot_system) == '1'
            and str(lot.announcement.external_id) == lot.announcement.number.split('-')[0])


def outcome_payload(watch):
    titles={'won':'🏆 Выиграли тендер','lost':'📄 Итоги: победил другой поставщик','review':'🔎 Итоги требуют повторной проверки',
              'not_listed':'📄 Не участвовали','cancelled':'🚫 Закупка отменена','unsuccessful':'📄 Закупка не состоялась'}
    esc=lambda value:escape(str(value or ''))
    names=list(tracked_items(watch.owner_id,watch.lot_id).values_list('profile__name',flat=True).distinct())
    text=f'{titles.get(watch.state,"📄 Итоги закупки")}\n\n<b>{esc(watch.lot.name[:600])}</b>\nЛот: <code>{esc(watch.lot.number)}</code>\nБИН компании: <code>{esc(watch.company_bin)}</code>'
    if names:text+='\n📂 '+esc(' · '.join(names)[:300])
    if watch.winner_bin:text+=f'\n\nПобедитель: {esc(watch.winner_name[:400])}\nБИН: <code>{esc(watch.winner_bin)}</code>'
    if watch.state=='not_listed':text+='\n\nПолнота списка участников подтверждена. Наш БИН не найден. Лот перенесён в «Не участвовали».'
    elif watch.state=='review':text+='\n\nРанее подтверждённый результат нельзя считать актуальным. Требуется повторная проверка протокола.'
    text+='\n\nРезультат проверен по опубликованному протоколу нужного лота.' if watch.state in ('won','lost') else ''
    buttons=[[{'text':'Открыть закупку ↗','url':watch.lot.portal_url}]]
    if watch.protocol_url:buttons.insert(0,[{'text':'Протокол итогов ↗','url':watch.protocol_url}])
    return {'text':text,'parse_mode':'HTML','link_preview_options':{'is_disabled':True},'reply_markup':{'inline_keyboard':buttons}}


def check_outcome(watch_id, lease):
    snapshot=LotOutcomeWatch.objects.filter(pk=watch_id,enabled=True,lease=lease,owner_id__in=eligible_user_ids()).select_related('lot__announcement').first()
    if not snapshot:return
    try:
        if not tracked_items(snapshot.owner_id,snapshot.lot_id).exists():return
        current_bin=company_bin_for(snapshot.owner_id)
        if not current_bin: result=Outcome('no_bin')
        elif current_bin!=snapshot.company_bin:return
        elif not supported_source(snapshot.lot):result=Outcome('unsupported')
        else:
            try:
                with source_client(snapshot.lot.announcement.source) as api:
                    result=OutcomePortal(api).outcome(snapshot.lot.announcement.external_id,snapshot.lot.announcement.number,snapshot.lot.number,snapshot.company_bin)
            except ProtocolFormatError as exc:
                result=Outcome('review');result.error=str(exc)[:300]
        if result.state=='not_listed' and not result.participants_complete:
            result=Outcome('review',protocol_url=result.protocol_url,fingerprint=result.fingerprint)
            result.error='Наш БИН не найден, но полнота списка участников не подтверждена. Требуется проверка.'
        with transaction.atomic():
            company=CompanySettings.objects.select_for_update().filter(owner_id=snapshot.owner_id).first()
            if (company.company_bin if company else '')!=snapshot.company_bin:return
            list(SearchProfile.objects.select_for_update().filter(owner_id=snapshot.owner_id).order_by('pk'))
            items=list(tracked_items(snapshot.owner_id,snapshot.lot_id).select_for_update())
            current=LotOutcomeWatch.objects.select_for_update().filter(pk=watch_id,enabled=True,lease=lease,generation=snapshot.generation).select_related('lot__announcement').first()
            if not current or not items:return
            from .models import Announcement
            if any(item.stage in ('interesting','preparing','awaiting_results') for item in items) and Announcement.objects.filter(
                    pk=current.lot.announcement_id,deadline__gt=timezone.now()).exists():
                return  # Acceptance reopened while the protocol request was running.
            old_state=current.state
            changed=(old_state,current.winner_bin)!=(result.state,result.winner_bin)
            current.state=result.state;current.winner_bin=result.winner_bin;current.winner_name=result.winner_name
            current.protocol_url=result.protocol_url;current.fingerprint=result.fingerprint
            current.checked_at=timezone.now();current.error=getattr(result,'error','')
            current.next_check_at=current.checked_at+timedelta(hours=24 if result.state in ('won','lost') else (6 if result.state in ('unsupported','no_bin') else 1))
            if changed:current.revision+=1
            current.save()
            if result.state in ('won','lost','cancelled','unsuccessful','not_listed'):
                for item in items:
                    if item.stage in ('contract','delivery','payment'):
                        # Preserve actual fulfillment stage, but expose revised verdict.
                        item.outcome=result.state if result.state in ('won','lost') else ''
                        item.save(update_fields=['outcome','updated_at'])
                        continue
                    if result.state in ('cancelled','unsuccessful') and item.stage in ('interesting','preparing','awaiting_results'):
                        continue
                    item.stage='contract' if result.state == 'won' else ('lost' if result.state == 'lost' else ('not_participated' if result.state=='not_listed' else 'applied'));item.outcome=result.state if result.state in ('won','lost') else ''
                    if result.state=='not_listed':item.result_tracking=False
                    item.save(update_fields=['stage','outcome','result_tracking','updated_at'])
            elif old_state in ('won','lost'):
                for item in items:
                    if item.stage in ('won','lost'):
                        item.stage='applied';item.outcome='';item.save(update_fields=['stage','outcome','updated_at'])
                    elif item.stage in ('contract','delivery','payment'):
                        item.outcome='';item.save(update_fields=['outcome','updated_at'])
            if result.state=='not_listed' and not tracked_items(current.owner_id,current.lot_id).exists():
                current.enabled=False
                current.next_check_at=None
                current.save(update_fields=['enabled','next_check_at','updated_at'])
            if changed and (result.state in ('won','lost','not_listed','cancelled','unsuccessful') or old_state in ('won','lost')):
                OutcomeDelivery.objects.filter(watch=current,state='pending').update(state='cancelled')
                OutcomeDelivery.objects.get_or_create(event_key=f'{current.pk}:{current.generation}:{current.revision}',
                    defaults={'watch':current,'generation':current.generation,'revision':current.revision,'payload':outcome_payload(current),
                        'state':'pending' if TelegramSubscription.objects.filter(owner_id=current.owner_id,enabled=True,chat_id__isnull=False).exists() else 'cancelled'})
    except Exception as exc:
        LotOutcomeWatch.objects.filter(pk=watch_id,lease=lease).update(error=str(exc)[:300] if isinstance(exc,SourceError) else 'Не удалось проверить итоги. Повторим позже.',next_check_at=timezone.now()+timedelta(hours=1))
    finally:
        LotOutcomeWatch.objects.filter(pk=watch_id,lease=lease).update(lease='',lease_until=None)


def deliver_outcomes():
    from django.conf import settings
    if not settings.TELEGRAM_BOT_TOKEN:return
    now=timezone.now()
    OutcomeDelivery.objects.filter(state='sending',updated_at__lt=now-timedelta(minutes=5)).update(state='uncertain',error='Отправка не подтверждена; повтор отключён.')
    deliveries=OutcomeDelivery.objects.filter(state='pending',watch__owner_id__in=eligible_user_ids()).filter(Q(retry_at__isnull=True)|Q(retry_at__lte=now)).select_related('watch')
    for delivery in deliveries[:20]:
        watch=delivery.watch
        terminal_absence = watch.state=='not_listed' and CollectionLot.objects.filter(
            profile__owner_id=watch.owner_id,lot_id=watch.lot_id,stage='not_participated',result_tracking=False).exists()
        if watch.generation!=delivery.generation or watch.revision!=delivery.revision or not (
                terminal_absence or (watch.enabled and tracked_items(watch.owner_id,watch.lot_id).exists())):
            OutcomeDelivery.objects.filter(pk=delivery.pk).update(state='cancelled');continue
        subscription=TelegramSubscription.objects.filter(owner_id=watch.owner_id,enabled=True,chat_id__isnull=False).first()
        if not subscription:
            OutcomeDelivery.objects.filter(pk=delivery.pk,state='pending').update(state='cancelled')
            continue
        if not OutcomeDelivery.objects.filter(pk=delivery.pk,state='pending').update(state='sending',updated_at=timezone.now()):continue
        try:
            result=telegram_api('sendMessage',dict(delivery.payload,chat_id=subscription.chat_id))
            OutcomeDelivery.objects.filter(pk=delivery.pk).update(state='sent',message_id=result['message_id'],error='')
        except TelegramError as exc:
            if exc.code==429:
                OutcomeDelivery.objects.filter(pk=delivery.pk).update(state='pending',retry_at=now+timedelta(seconds=max(1,exc.retry_after)),error='Лимит Telegram.');return
            if exc.code==403:
                TelegramSubscription.objects.filter(pk=subscription.pk).update(enabled=False)
                from .workflow_notifications import cancel_pending
                cancel_pending(watch.owner_id)
            OutcomeDelivery.objects.filter(pk=delivery.pk).update(state='uncertain' if exc.code==0 else 'error',error=f'Telegram: {exc.code}')


def move_closed_unsubmitted():
    from .models import UserLotWorkflow
    from .workflow_state import update_workflow, reconcile_workflow_states
    now=timezone.now()
    enroll_participation_checks()
    records=UserLotWorkflow.objects.filter(owner_id__in=eligible_user_ids(),result_tracking=False)
    candidates=records.filter(Q(stage__in=['new','interesting'],keep_after_deadline=False,
        lot__announcement__deadline__lte=now)|Q(stage='not_participated',
        expired_from_stage__in=['new','interesting'],lot__announcement__deadline__gt=now))
    for pk in list(candidates.values_list('pk',flat=True)):
        with transaction.atomic():
            # Same lock order as user actions; recheck after locking to avoid undoing a manual move.
            owner_id=UserLotWorkflow.objects.values_list('owner_id',flat=True).get(pk=pk)
            list(SearchProfile.objects.select_for_update().filter(owner_id=owner_id).order_by('pk'))
            state=candidates.select_for_update().filter(pk=pk).first()
            if not state:
                continue
            if state.stage == 'not_participated':
                update_workflow(state.owner_id,state.lot_id,stage=state.expired_from_stage,expired_from_stage='')
            else:
                update_workflow(state.owner_id,state.lot_id,expired_from_stage=state.stage,stage='not_participated')
    # A genuinely extended deadline starts a new acceptance period.
    records.filter(keep_after_deadline=True,lot__announcement__deadline__gt=now).update(keep_after_deadline=False)
    reconcile_workflow_states()

