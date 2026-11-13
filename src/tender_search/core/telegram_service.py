from .access import eligible_user_ids
from datetime import timedelta
from hashlib import sha256
from html import escape
import secrets
from zoneinfo import ZoneInfo
import httpx
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from tender_search.client import ZakupClient, SourceError
from .models import TelegramSubscription, TelegramCursor, TelegramDelivery, TelegramSeen, SearchProfile, WebSearch, TelegramContact
from .source import RedisGate

ALMATY = ZoneInfo('Asia/Almaty')

def in_work_hours(now=None):
    current = (now or timezone.now()).astimezone(ALMATY)
    return 7 <= current.hour < 19

class TelegramError(Exception):
    def __init__(self, code=0, retry_after=60):
        self.code, self.retry_after = code, retry_after
        super().__init__(f'Telegram API error {code}')

def telegram_api(method, payload):
    if not settings.TELEGRAM_BOT_TOKEN:
        raise TelegramError()
    # Keep token-bearing URLs out of HTTPX logs and exception messages.
    import logging
    logging.getLogger('httpx').setLevel(logging.WARNING)
    try:
        with httpx.Client(timeout=25) as client:
            response = client.post(f'https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/{method}', json=payload)
            data = response.json()
    except (httpx.HTTPError, ValueError):
        raise TelegramError() from None
    if not data.get('ok'):
        raise TelegramError(data.get('error_code', response.status_code), data.get('parameters', {}).get('retry_after', 60))
    return data['result']

def connection_link(owner):
    token = secrets.token_urlsafe(24)
    subscription, _ = TelegramSubscription.objects.get_or_create(owner=owner)
    subscription.link_hash = sha256(token.encode()).hexdigest()
    subscription.link_expires_at = timezone.now() + timedelta(minutes=10)
    subscription.save(update_fields=['link_hash', 'link_expires_at', 'updated_at'])
    return f'https://t.me/{settings.TELEGRAM_BOT_USERNAME}?start={token}'

def record_contact(chat, text=None):
    import re
    if chat.get('type') != 'private' or not isinstance(chat.get('id'), int) or isinstance(chat['id'], bool):
        return
    now = timezone.now()
    username = chat.get('username') or ''
    if not isinstance(username, str) or not re.fullmatch(r'[A-Za-z0-9_]{1,64}', username):
        username = ''
    values = {'username': username, 'first_name': str(chat.get('first_name') or '')[:255],
              'last_name': str(chat.get('last_name') or '')[:255]}
    if text is not None:
        values['last_seen_at'] = now
        if text == '/stop':
            values['enabled'] = False
        elif text == '/start' or text.startswith('/start '):
            values['enabled'] = True
    contact, created = TelegramContact.objects.get_or_create(chat_id=chat['id'],
        defaults=dict(values, first_seen_at=now if text is not None else None))
    if not created:
        if text is not None and contact.first_seen_at is None:
            values['first_seen_at'] = now
        TelegramContact.objects.filter(pk=contact.pk).update(**values)


def handle_update(update):
    if isinstance(update.get('callback_query'),dict):
        from .telegram_actions import handle_callback
        handle_callback(update['callback_query'])
        return
    message = update.get('message') or {}
    chat = message.get('chat') or {}
    if chat.get('type') != 'private' or not isinstance(chat.get('id'), int):
        return
    text = message.get('text', '').strip()
    record_contact(chat, text)
    if text == '/stop':
        TelegramSubscription.objects.filter(chat_id=chat['id']).update(enabled=False)
        from .workflow_notifications import cancel_pending
        for owner_id in TelegramSubscription.objects.filter(chat_id=chat['id']).values_list('owner_id',flat=True):
            cancel_pending(owner_id)
        return
    if not text.startswith('/start '):
        return
    token = text.split(' ', 1)[1]
    with transaction.atomic():
        sub = TelegramSubscription.objects.select_for_update().filter(link_hash=sha256(token.encode()).hexdigest(), link_expires_at__gt=timezone.now()).first()
        if not sub or TelegramSubscription.objects.filter(chat_id=chat['id']).exclude(pk=sub.pk).exists():
            return
        sub.chat_id, sub.enabled, sub.link_hash, sub.link_expires_at = chat['id'], True, '', None
        sub.save()
    # No unsolicited confirmation outside the delivery window; web settings show connection immediately.

def poll_updates():
    cursor, _ = TelegramCursor.objects.get_or_create(pk=1)
    updates = telegram_api('getUpdates', {'offset':cursor.offset, 'timeout':10, 'allowed_updates':['message','callback_query']})
    for update in updates:
        handle_update(update)
        cursor.offset = max(cursor.offset, update['update_id']+1)
        cursor.save(update_fields=['offset'])

class WorkHoursGate:
    def __init__(self): self.gate = RedisGate()
    def acquire(self):
        if not in_work_hours(): raise SourceError('Проверка отложена до 07:00 по Алматы.')
        self.gate.acquire()
        if not in_work_hours(): raise SourceError('Проверка отложена до 07:00 по Алматы.')

def announcement_key(lot):
    a = lot.announcement
    identity = f'{lot.raw.get("system_id", "zakup")}:{a.number}' if a.number else f'zakup:{a.source_id}'
    if a.source == 'fms_ecc': identity = f'7:{a.number}'
    elif a.source != 'zakup': identity = f'{a.source}:{a.source_id}'
    return sha256(identity.encode()).hexdigest()

def schedule_profiles():
    if not settings.TELEGRAM_BOT_TOKEN or not in_work_hours(): return
    from .tasks import monitor_profile
    now = timezone.now()
    owners = TelegramSubscription.objects.filter(enabled=True, chat_id__isnull=False).values('owner_id')
    candidates = SearchProfile.objects.filter(enabled=True, owner_id__in=owners).filter(owner_id__in=eligible_user_ids()).filter(Q(next_monitor_at__isnull=True)|Q(next_monitor_at__lte=now)).filter(Q(monitor_lease_until__isnull=True)|Q(monitor_lease_until__lt=now))
    for profile in candidates[:100]:
        token = secrets.token_hex(16)
        claimed = SearchProfile.objects.filter(pk=profile.pk).filter(Q(monitor_lease_until__isnull=True)|Q(monitor_lease_until__lt=now)).update(monitor_lease=token, monitor_lease_until=now+timedelta(minutes=25))
        if claimed:
            try: monitor_profile.delay(profile.pk, token)
            except Exception:
                SearchProfile.objects.filter(pk=profile.pk,monitor_lease=token).update(monitor_lease='',monitor_lease_until=None,monitor_error='Очередь недоступна',next_monitor_at=now+timedelta(minutes=5))

def scan_profile(profile_id, lease):
    from .forms import SearchForm
    from .search_service import execute_search
    profile = SearchProfile.objects.filter(pk=profile_id,monitor_lease=lease,enabled=True,owner_id__in=eligible_user_ids()).first()
    if not profile: return
    try:
        if not in_work_hours() or not TelegramSubscription.objects.filter(owner=profile.owner, enabled=True,chat_id__isnull=False).exists(): return
        values = profile.filters.get('form', {})
        from django.http import QueryDict
        data = QueryDict('',mutable=True)
        for key,value in values.items():
            if key.startswith('_'): continue
            if isinstance(value,list): data.setlist(key,value)
            else: data[key] = value
        form = SearchForm(data)
        if not form.is_valid(): raise SourceError('Проверьте условия сохранённой подборки.')
        specification = form.specification()
        specification["monitor_profile_id"] = profile.pk
        job = WebSearch.objects.create(owner=profile.owner,specification=specification)
        if settings.USE_LOCAL_INDEX:
            from .index_service import execute_local_search
            execute_local_search(job.pk)
        else:
            with ZakupClient(gate=WorkHoursGate()) as api: execute_search(job.pk,api)
        job.refresh_from_db()
        if job.error: raise SourceError(job.error)
        if settings.USE_LOCAL_INDEX:
            coverage = job.source_counts.get('__index__', {})
            if not coverage.get('ready'):
                raise SourceError('База активных закупок заполняется или требует обновления. Рассылка ожидает полного обхода.')
            if not in_work_hours():
                return
        with transaction.atomic():
            current = SearchProfile.objects.select_for_update().get(pk=profile_id)
            if current.monitor_lease != lease or current.filters != profile.filters or not current.enabled: return
            baseline = current.index_baseline_at is None if settings.USE_LOCAL_INDEX else current.baseline_at is None
            from .models import UserLotWorkflow
            archived_lots = set(UserLotWorkflow.objects.filter(owner=current.owner,stage__in=('archived','not_participated')).values_list('lot_id',flat=True))
            for hit in job.hits.select_related('lot__announcement','lot__customer').iterator():
                lot = hit.lot
                if lot.pk in archived_lots:
                    continue
                key = announcement_key(lot)
                _, new = TelegramSeen.objects.get_or_create(profile=current,announcement_key=key)
                eligible = True
                if settings.USE_LOCAL_INDEX and not baseline:
                    from .index_service import date_value
                    published = date_value(lot.raw.get('announcement_publish_date'))
                    eligible = bool(published and published >= current.index_baseline_at)
                if new and not baseline and eligible:
                    TelegramDelivery.objects.get_or_create(owner=current.owner,announcement_key=key,defaults={'profile':current,'lot':lot})
            current.baseline_at = current.baseline_at or timezone.now()
            if settings.USE_LOCAL_INDEX:
                current.index_baseline_at = current.index_baseline_at or timezone.now()
            current.monitor_error = (coverage.get('scope_note','') if settings.USE_LOCAL_INDEX else '') if not job.complete else ''
            current.save(update_fields=['baseline_at','index_baseline_at','monitor_error','updated_at'])
    except Exception as exc:
        # Do not log exceptions that might embed credentials or external response bodies.
        SearchProfile.objects.filter(pk=profile_id,monitor_lease=lease).update(monitor_error=str(exc)[:200] if isinstance(exc,SourceError) else 'Ошибка проверки. Повторим позже.')
    finally:
        current = SearchProfile.objects.filter(pk=profile_id,monitor_lease=lease).first()
        if current:
            SearchProfile.objects.filter(pk=profile_id,monitor_lease=lease).update(last_monitor_at=timezone.now(),next_monitor_at=timezone.now()+timedelta(minutes=max(1,current.interval_minutes)),monitor_lease='',monitor_lease_until=None)

def message_for(delivery):
    from .deadlines import telegram_remaining_time
    lot, profile = delivery.lot, delivery.profile
    esc = lambda text, length: escape(str(text or '')[:length])
    deadline = lot.announcement.deadline.astimezone(ALMATY).strftime('%d.%m.%Y %H:%M') if lot.announcement.deadline else 'Уточните на портале'
    amount = f'{lot.amount:,.2f}'.replace(',',' ')+' ₸' if lot.amount is not None else 'Не указана'
    from tender_search.matching import match_lot
    specification = profile.filters.get('specification', {})
    matched = match_lot(lot.name, lot.description, specification) if specification.get('terms') else []
    matched_line = f'🔎 Совпало: <b>{esc(" · ".join(matched),500)}</b>\n\n' if matched else ''
    body = (f'📂 <b>Подборка: {esc(profile.name,200)}</b>\n🔔 Новая закупка\n{matched_line}'
            f'📦 Лот: <code>{esc(lot.number or "Не указан",100)}</code>\n'
            f'Объявление: <code>{esc(lot.announcement.number or "Не указано",100)}</code>\n\n'
            f'<b>{esc(lot.announcement.name or lot.name,700)}</b>\n\n'
            f'📦 Лот: {esc(lot.name,500)}\n💰 Сумма лота: <b>{esc(amount,100)}</b>\n'
            f'🏢 {esc(lot.customer.name if lot.customer else "Заказчик не указан",350)}\n'
            f'⏳ Приём до: <b>{deadline}</b> (Алматы)\n'
            f'{telegram_remaining_time(lot.announcement.deadline)}'
            f'\nПроверьте актуальные условия на портале.')
    url = lot.portal_url
    buttons=[[{'text':('Открыть на госзакупе ↗' if lot.announcement.source == 'zakup' else f'Открыть на {lot.portal_label} ↗'),'url':url}]]
    from .telegram_actions import callback_data
    action=callback_data(delivery)
    if action:buttons.insert(0,[{'text':'⭐ В интересные','callback_data':action}])
    return {'text':body,'parse_mode':'HTML','link_preview_options':{'is_disabled':True},'reply_markup':{'inline_keyboard':buttons}}

def deliver_pending():
    if not settings.TELEGRAM_BOT_TOKEN or not in_work_hours(): return
    now = timezone.now()
    # A process interruption after claiming can mean Telegram accepted a message. Never resend blindly.
    TelegramDelivery.objects.filter(state='sending',updated_at__lt=now-timedelta(minutes=5)).update(state='uncertain',error='Не удалось подтвердить доставку; автоматический повтор отключён.')
    deliveries=TelegramDelivery.objects.filter(state='pending',profile__enabled=True,owner_id__in=eligible_user_ids()).filter(Q(retry_at__isnull=True)|Q(retry_at__lte=now)).select_related('lot__announcement','lot__customer','profile')
    for delivery in deliveries[:20]:
        if not in_work_hours(): return
        from .models import UserLotWorkflow
        raw_status=(delivery.lot.raw or {}).get('status') or {}
        status_id=raw_status.get('id') if isinstance(raw_status,dict) else raw_status
        status_id=status_id or (delivery.lot.raw or {}).get('status_id')
        if (UserLotWorkflow.objects.filter(owner_id=delivery.owner_id,lot_id=delivery.lot_id,stage__in=('archived','not_participated')).exists()
                or (delivery.lot.announcement.deadline and delivery.lot.announcement.deadline<=timezone.now())
                or (status_id and str(status_id) not in ('6','7','9'))):
            TelegramDelivery.objects.filter(pk=delivery.pk,state='pending').update(state='cancelled')
            continue
        sub=TelegramSubscription.objects.filter(owner=delivery.owner,enabled=True,chat_id__isnull=False).first()
        if not sub:
            TelegramDelivery.objects.filter(pk=delivery.pk,state='pending').update(state='cancelled')
            continue
        if not TelegramDelivery.objects.filter(pk=delivery.pk,state='pending').update(state='sending',updated_at=timezone.now()): continue
        if not delivery.interest_token:
            from uuid import uuid4
            delivery.interest_token=uuid4()
            TelegramDelivery.objects.filter(pk=delivery.pk,state='sending').update(interest_token=delivery.interest_token)
        try:
            result=telegram_api('sendMessage',dict(message_for(delivery),chat_id=sub.chat_id))
            TelegramDelivery.objects.filter(pk=delivery.pk).update(state='sent',message_id=result['message_id'],error='')
        except TelegramError as exc:
            if exc.code==429:
                TelegramDelivery.objects.filter(pk=delivery.pk).update(state='pending',retry_at=timezone.now()+timedelta(seconds=max(1,exc.retry_after)),error='Лимит Telegram; отправим позже.')
                return
            if exc.code==403:
                TelegramSubscription.objects.filter(pk=sub.pk).update(enabled=False)
                from .workflow_notifications import cancel_pending
                cancel_pending(delivery.owner_id)
            TelegramDelivery.objects.filter(pk=delivery.pk).update(state='uncertain' if exc.code==0 else 'error',error='Доставка не подтверждена.' if exc.code==0 else f'Telegram: {exc.code}')
        except Exception:
            TelegramDelivery.objects.filter(pk=delivery.pk).update(state='error',error='Ошибка формирования сообщения.')


