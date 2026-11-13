"""Durable notifications for verified events, separate from status watch lifetime."""
from .access import eligible_user_ids
from datetime import timedelta
from hashlib import sha256
from html import escape
from django.conf import settings
from django.db.models import Q
from django.utils import timezone
from .models import (WorkflowDelivery, TelegramSubscription, CompanySettings,
                     UserLotWorkflow, TelegramDelivery, StatusDelivery, OutcomeDelivery)
from .telegram_service import telegram_api, TelegramError

TITLES = {'submitted': '✅ Заявка подана', 'signed': '📝 Договор подписан', 'paid': '💰 Оплата получена'}


def connected(owner_id):
    return TelegramSubscription.objects.filter(owner_id=owner_id,enabled=True,chat_id__isnull=False).exists()


def cancel_pending(owner_id):
    TelegramDelivery.objects.filter(owner_id=owner_id,state='pending').update(state='cancelled')
    StatusDelivery.objects.filter(watch__owner_id=owner_id,state='pending').update(state='cancelled')
    OutcomeDelivery.objects.filter(watch__owner_id=owner_id,state='pending').update(state='cancelled')
    WorkflowDelivery.objects.filter(owner_id=owner_id,state='pending').update(state='cancelled')


def queue_event(owner_id, lot, company_bin, kind, reference, label=None):
    """Call inside the transaction confirming the event; no Telegram request here."""
    if kind not in TITLES or not company_bin or not reference:
        return
    key=sha256(f'{owner_id}:{lot.pk}:{company_bin}:{kind}:{reference}'.encode()).hexdigest()
    if kind=='submitted':label=label or reference
    esc=lambda value:escape(str(value or ''))
    contract=f'Договор № {label}. ' if label else ''
    detail={'submitted': f'Заявка № {label}. Подача подтверждена на Госзакупе.',
            'signed': contract+'Подписание договора подтверждено; лот перешёл в поставку.',
            'paid': contract+'Подтверждены полное исполнение и оплата. Лот перенесён в «Выиграли»; через 30 дней перейдёт в архив.'}[kind]
    payload={'text':f'<b>{TITLES[kind]}</b>\n\nЛот: <code>{esc(lot.number)}</code>\n<b>{esc(lot.name[:500])}</b>\nБИН: <code>{esc(company_bin)}</code>\n\n{esc(detail)}',
             'parse_mode':'HTML','link_preview_options':{'is_disabled':True},
             'reply_markup':{'inline_keyboard':[[{'text':'Открыть на Госзакупе ↗','url':lot.portal_url}]]}}
    WorkflowDelivery.objects.get_or_create(event_key=key,defaults={'owner_id':owner_id,'lot':lot,
        'company_bin':company_bin,'kind':kind,'payload':payload,'state':'pending' if connected(owner_id) else 'cancelled'})


def deliver_workflow_notifications():
    if not settings.TELEGRAM_BOT_TOKEN:
        return
    now=timezone.now()
    WorkflowDelivery.objects.filter(state='sending',updated_at__lt=now-timedelta(minutes=5)).update(
        state='uncertain',error='Доставка не подтверждена; повтор отключён.')
    rows=WorkflowDelivery.objects.filter(state='pending',owner_id__in=eligible_user_ids()).filter(Q(retry_at__isnull=True)|Q(retry_at__lte=now)).select_related('owner').order_by('created_at','pk')
    for delivery in rows[:20]:
        workflow=UserLotWorkflow.objects.filter(owner_id=delivery.owner_id,lot_id=delivery.lot_id).first()
        bin_matches=CompanySettings.objects.filter(owner_id=delivery.owner_id,company_bin=delivery.company_bin).exists()
        relevant=bool(workflow and (
            (delivery.kind=='submitted' and workflow.stage=='applied') or
            (delivery.kind=='signed' and workflow.stage=='delivery' and workflow.outcome=='won') or
            (delivery.kind=='paid' and workflow.stage in ('won','archived') and workflow.completed_at)))
        sub=TelegramSubscription.objects.filter(owner_id=delivery.owner_id,enabled=True,chat_id__isnull=False).first()
        if not delivery.owner.is_active or not bin_matches or not relevant or not sub or delivery.created_at<now-timedelta(days=1):
            WorkflowDelivery.objects.filter(pk=delivery.pk,state='pending').update(state='cancelled')
            continue
        if not WorkflowDelivery.objects.filter(pk=delivery.pk,state='pending').update(state='sending',updated_at=now):
            continue
        try:
            result=telegram_api('sendMessage',dict(delivery.payload,chat_id=sub.chat_id))
            WorkflowDelivery.objects.filter(pk=delivery.pk).update(state='sent',message_id=result['message_id'],error='')
        except TelegramError as exc:
            if exc.code==429:
                WorkflowDelivery.objects.filter(pk=delivery.pk).update(state='pending',retry_at=now+timedelta(seconds=max(1,exc.retry_after)),error='Лимит Telegram.')
                return
            if exc.code==403:
                TelegramSubscription.objects.filter(pk=sub.pk).update(enabled=False)
                cancel_pending(delivery.owner_id)
            WorkflowDelivery.objects.filter(pk=delivery.pk).update(state='uncertain' if exc.code==0 else 'error',error=f'Telegram: {exc.code}')

