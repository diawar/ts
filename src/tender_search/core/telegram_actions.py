"""Actions bound to an actually delivered private notification and its recipient."""
import re
from .access import eligible_user_ids
from uuid import UUID
from django.db import transaction
from django.utils import timezone
from .models import TelegramSubscription,TelegramDelivery,SearchProfile,UserLotWorkflow
from .workflow_state import add_to_participation,LOCKED_STAGES


def callback_data(delivery):
    if not delivery.interest_token:
        return None
    return f'interest:{delivery.pk}:{delivery.interest_token.hex}'


def apply_interest(query):
    match=re.fullmatch(r'interest:([1-9][0-9]{0,18}):([0-9a-f]{32})',str(query.get('data','')))
    message=query.get('message') or {}
    chat=message.get('chat') or {}
    sender=query.get('from') or {}
    chat_id=chat.get('id')
    message_id=message.get('message_id')
    if (not match or chat.get('type')!='private' or not isinstance(chat_id,int) or isinstance(chat_id,bool)
            or isinstance(sender.get('id'),bool) or sender.get('id')!=chat_id
            or not isinstance(message_id,int) or isinstance(message_id,bool) or message_id<=0
            or int(match[1])>9223372036854775807):
        return 'Кнопка недоступна для этого сообщения.'
    sub=TelegramSubscription.objects.filter(chat_id=chat_id,enabled=True,owner_id__in=eligible_user_ids()).first()
    if not sub:
        return 'Подключите Telegram в настройках приложения.'
    with transaction.atomic():
        list(SearchProfile.objects.select_for_update().filter(owner_id=sub.owner_id).order_by('pk'))
        delivery=TelegramDelivery.objects.select_for_update().select_related('lot__announcement').filter(
            pk=int(match[1]),interest_token=UUID(hex=match[2]),owner_id=sub.owner_id,
            state='sent',message_id=message['message_id']).first()
        if not delivery or not TelegramSubscription.objects.filter(pk=sub.pk,enabled=True,chat_id=chat_id,owner_id__in=eligible_user_ids()).exists():
            return 'Кнопка недоступна для этого сообщения.'
        state=UserLotWorkflow.objects.select_for_update().filter(owner_id=sub.owner_id,lot_id=delivery.lot_id).first()
        if state and state.stage=='preparing':
            return 'Лот уже на этапе «В работе».'
        if state and state.stage in LOCKED_STAGES:
            return 'Лот уже на следующем этапе. Этап сохранён.'
        if delivery.lot.announcement.deadline and delivery.lot.announcement.deadline<=timezone.now():
            return 'Приём заявок завершён. Лот не перенесён.'
        raw=delivery.lot.raw or {}
        status=raw.get('status') or {}
        status_id=(status.get('id') if isinstance(status,dict) else status) or raw.get('status_id')
        if status_id and str(status_id) not in ('6','7','9'):
            return 'Приём по этому лоту недоступен. Лот не перенесён.'
        add_to_participation(sub.owner_id,delivery.lot_id)
        return 'Лот добавлен на этап «В работе».'


def handle_callback(query):
    from .telegram_service import telegram_api,TelegramError
    text=apply_interest(query)
    query_id=query.get('id')
    if isinstance(query_id,str) and query_id:
        try:
            telegram_api('answerCallbackQuery',{'callback_query_id':query_id,'text':text,'show_alert':True})
        except TelegramError:
            # Expired callback confirmations must not block all incoming updates.
            pass

