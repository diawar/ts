"""Manual archive actions and internal, evidence-driven fulfillment transitions."""
import secrets
from datetime import timedelta
from django.db import transaction
from django.db.models import Exists, OuterRef
from django.utils import timezone
from .models import UserLotWorkflow, CollectionLot, SearchProfile, LotOutcomeWatch, OutcomeDelivery, TelegramDelivery
from .workflow_state import update_workflow

ARCHIVABLE = ('won', 'lost', 'not_participated')


def actions_for(state):
    if not state:
        return []
    actions = []
    if state.stage in ARCHIVABLE:
        actions.append({'value':'archive', 'label':'В архив'})
    if state.stage == 'archived':
        actions.append({'value':'restore', 'label':'Вернуть из архива'})
    return actions


def pause_outcome(owner_id, lot_id):
    for watch in LotOutcomeWatch.objects.select_for_update().filter(owner_id=owner_id, lot_id=lot_id):
        watch.enabled = False
        watch.generation = secrets.token_hex(16)
        watch.lease = ''; watch.lease_until = None; watch.next_check_at = None
        watch.save(update_fields=['enabled','generation','lease','lease_until','next_check_at','updated_at'])
        OutcomeDelivery.objects.filter(watch=watch,state='pending').update(state='cancelled')


def advance(owner_id, lot_id, action):
    if action not in ('archive', 'restore'):
        raise ValueError('Этот этап меняется автоматически после подтверждения данных.')
    return _advance(owner_id, lot_id, action)


def accept_uploaded_act(owner_id, lot_id):
    return _advance(owner_id, lot_id, 'payment', automatic=True)


def complete_payment(owner_id, lot_id):
    return _advance(owner_id, lot_id, 'paid', automatic=True)


def _advance(owner_id, lot_id, action, *, automatic=False):
    with transaction.atomic():
        list(SearchProfile.objects.select_for_update().filter(owner_id=owner_id).order_by('pk'))
        state = UserLotWorkflow.objects.select_for_update().filter(owner_id=owner_id,lot_id=lot_id).first()
        if not state or not CollectionLot.objects.filter(profile__owner_id=owner_id,lot_id=lot_id).exists():
            raise ValueError('Лот отсутствует в рабочем пространстве.')
        allowed = ({'delivery': 'payment', 'payment': 'paid'}.get(state.stage) == action
                   if automatic else action in {item['value'] for item in actions_for(state)})
        if not allowed:
            raise ValueError('Действие недоступно на текущем этапе. Обновите страницу.')
        if action in ('payment', 'paid'):
            from .models import LotShipment
            shipment = LotShipment.objects.filter(owner_id=owner_id, lot_id=lot_id).exclude(act='').first()
            if not shipment or not shipment.shipped_at or not shipment.act_uploaded_at:
                raise ValueError('Прикрепите акт в карточке лота для перехода к оплате.')
        if action == 'paid':
            from .models import ContractWatch
            confirmed = ContractWatch.objects.filter(workflow=state,state='paid',evidence__state='paid',
                    checked_at__gte=timezone.now()-timedelta(minutes=10)).first()
            if not confirmed:
                raise ValueError('Полная оплата ещё не подтверждена проверкой договора.')
        previous = state.stage
        if action == 'restore':
            previous = state.archived_from_stage
            if previous not in (*ARCHIVABLE, 'contract', 'payment'):
                raise ValueError('Не удалось определить этап для восстановления.')
            tracking = state.archived_result_tracking and previous in ('lost','contract','payment')
            update_workflow(owner_id,lot_id,stage=previous,result_tracking=tracking,
                archived_at=None,archived_from_stage='',archived_result_tracking=False,
                completed_at=state.completed_at if previous=='won' else None)
            if tracking:
                # Keep the known verdict: restoring must not send the same result again.
                LotOutcomeWatch.objects.filter(owner_id=owner_id,lot_id=lot_id).update(
                    enabled=True,lease='',lease_until=None,next_check_at=None)
            from .contract_service import invalidate
            invalidate(owner_id, lot_id)
        elif action in ('archive','paid'):
            if action=='paid':
                update_workflow(owner_id,lot_id,stage='won',outcome='won',result_tracking=False,
                    completed_at=timezone.now())
            else:
                update_workflow(owner_id,lot_id,stage='archived',result_tracking=False,
                    archived_from_stage=previous,archived_result_tracking=state.result_tracking,
                    archived_at=timezone.now())
            pause_outcome(owner_id,lot_id)
            from .models import ContractWatch
            ContractWatch.objects.filter(workflow=state).update(generation=secrets.token_hex(16),lease='',lease_until=None,next_check_at=None)
            TelegramDelivery.objects.filter(owner_id=owner_id,lot_id=lot_id,state='pending').update(state='cancelled')
            if action == 'paid':
                from .workflow_notifications import queue_event
                from .outcome_service import company_bin_for
                evidence=confirmed.evidence
                queue_event(owner_id,state.lot,company_bin_for(owner_id),'paid',
                    evidence.get('contract_id') or evidence.get('number'),evidence.get('number'))
        else:
            update_workflow(owner_id,lot_id,stage=action,result_tracking=True)
            from .contract_service import invalidate
            invalidate(owner_id, lot_id)
        return UserLotWorkflow.objects.values_list('stage',flat=True).get(pk=state.pk)


def archive_due():
    """Archive terminal deals 30 days after entering that stage, not last polling."""
    cutoff=timezone.now()-timedelta(days=30)
    due=UserLotWorkflow.objects.filter(stage__in=ARCHIVABLE,stage_entered_at__lte=cutoff).filter(
        Exists(CollectionLot.objects.filter(profile__owner_id=OuterRef('owner_id'),lot_id=OuterRef('lot_id'))))
    for pk,owner_id in list(due.order_by('stage_entered_at','pk').values_list('pk','owner_id')[:200]):
        with transaction.atomic():
            list(SearchProfile.objects.select_for_update().filter(owner_id=owner_id).order_by('pk'))
            state=due.select_for_update().filter(pk=pk).first()
            if state and CollectionLot.objects.filter(profile__owner_id=owner_id,lot_id=state.lot_id).exists():
                _advance(owner_id,state.lot_id,'archive')
