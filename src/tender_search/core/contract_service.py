"""Leased, read-only contract monitoring with fenced personal workflow updates."""
import secrets
from .access import eligible_user_ids
from datetime import timedelta
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from .models import ContractWatch, UserLotWorkflow, CompanySettings, SearchProfile, LotOutcomeWatch
from .source import source_client
from .contract_source import contract_evidence, ContractReview
from .outcome_service import company_bin_for, supported_source
from .workflow_state import update_workflow

STAGES = ('contract', 'delivery', 'payment')
ORDER = {'contract': 1, 'delivery': 2, 'payment': 3}


def invalidate(owner_id, lot_id=None):
    rows = ContractWatch.objects.filter(workflow__owner_id=owner_id)
    if lot_id is not None:
        rows = rows.filter(workflow__lot_id=lot_id)
    rows.update(generation=secrets.token_hex(16), lease='', lease_until=None,
                next_check_at=None, state='pending', error='', evidence={})


def schedule_contracts():
    from .tasks import check_lot_contract
    now = timezone.now()
    states = UserLotWorkflow.objects.filter(owner_id__in=eligible_user_ids(), outcome='won', stage__in=STAGES)
    for state in states.filter(contractwatch__isnull=True)[:100]:
        ContractWatch.objects.get_or_create(workflow=state, defaults={'company_bin': company_bin_for(state.owner_id)})
    due = ContractWatch.objects.filter(workflow__in=states).filter(
        Q(next_check_at__isnull=True) | Q(next_check_at__lte=now)).filter(
        Q(lease_until__isnull=True) | Q(lease_until__lte=now))
    for watch in due.order_by('next_check_at', 'pk')[:20]:
        lease = secrets.token_hex(16)
        claimed = ContractWatch.objects.filter(pk=watch.pk).filter(
            Q(lease_until__isnull=True) | Q(lease_until__lte=now)).update(lease=lease, lease_until=now+timedelta(minutes=10))
        if claimed:
            try:
                check_lot_contract.delay(watch.pk, lease)
            except Exception:
                ContractWatch.objects.filter(pk=watch.pk, lease=lease).update(lease='', lease_until=None,
                    next_check_at=now+timedelta(minutes=5), error='Очередь проверки договоров недоступна.')


def confirmed_outcome(state, company_bin, now):
    return LotOutcomeWatch.objects.filter(owner_id=state.owner_id, lot_id=state.lot_id,
        enabled=True, company_bin=company_bin, winner_bin=company_bin, state='won',
        checked_at__gte=now-timedelta(hours=26)).first()


def check_contract(watch_id, lease):
    watch = ContractWatch.objects.filter(pk=watch_id, lease=lease, workflow__owner_id__in=eligible_user_ids()).select_related(
        'workflow__lot__announcement').first()
    if not watch:
        return
    state = watch.workflow
    now = timezone.now()
    company_bin = company_bin_for(state.owner_id)
    verdict = confirmed_outcome(state, company_bin, now) if company_bin else None
    try:
        if state.stage not in STAGES or state.outcome != 'won':
            return
        try:
            if not company_bin:
                result = {'state': 'no_bin', 'message': 'Укажите БИН компании в настройках.'}
            elif not supported_source(state.lot):
                result = {'state': 'unsupported', 'message': 'Автоматическая проверка договоров этой площадки пока не поддерживается.'}
            elif not verdict:
                result = {'state': 'review', 'message': 'Ожидается актуальное подтверждение победы по БИН в протоколе.'}
            else:
                with source_client('zakup') as api:
                    result = contract_evidence(api, state.lot, company_bin, now)
        except ContractReview as exc:
            result = {'state': 'review', 'message': str(exc)[:300]}
        except Exception:
            result = {'state': 'error', 'message': 'Не удалось проверить договор. Повторим позже.'}
        with transaction.atomic():
            company = CompanySettings.objects.select_for_update().filter(owner_id=state.owner_id).first()
            if (company.company_bin if company else '') != company_bin:
                return
            list(SearchProfile.objects.select_for_update().filter(owner_id=state.owner_id).order_by('pk'))
            current = UserLotWorkflow.objects.select_for_update().get(pk=state.pk)
            active = ContractWatch.objects.select_for_update().filter(pk=watch.pk, lease=lease, generation=watch.generation).first()
            if not active or current.stage != state.stage or current.updated_at != state.updated_at or current.outcome != 'won':
                return
            fresh = confirmed_outcome(current, company_bin, timezone.now()) if company_bin else None
            if result['state'] in ('contract','delivery','payment','paid') and (
                    not fresh or not verdict or (fresh.generation, fresh.revision) != (verdict.generation, verdict.revision)):
                return
            active.company_bin = company_bin
            active.state = result['state']; active.evidence = result
            active.error = result.get('message', '') if result['state'] != 'waiting' else ''
            active.checked_at = timezone.now(); active.next_check_at = active.checked_at+timedelta(hours=1)
            active.save()
            target = result['state']
            from .models import LotShipment
            if target in ('payment', 'paid') and not LotShipment.objects.filter(owner_id=current.owner_id, lot_id=current.lot_id,
                    shipped_at__isnull=False,act_uploaded_at__isnull=False).exclude(act='').exists():
                target = 'delivery'
            if target == 'paid':
                from .fulfillment import complete_payment
                # All exact subjects executed and paid: recorded evidence permits skipping elapsed stages.
                if current.stage != 'payment':
                    update_workflow(current.owner_id,current.lot_id,stage='payment',result_tracking=True)
                complete_payment(current.owner_id,current.lot_id)
            elif target in ORDER and ORDER[target] > ORDER[current.stage]:
                update_workflow(current.owner_id,current.lot_id,stage=target,result_tracking=True)
                if target == 'delivery' and result['state'] == 'delivery':
                    from .workflow_notifications import queue_event
                    queue_event(current.owner_id,current.lot,company_bin,'signed',
                        result.get('contract_id') or result.get('number'),result.get('number'))
    finally:
        ContractWatch.objects.filter(pk=watch_id,lease=lease).update(lease='',lease_until=None)

