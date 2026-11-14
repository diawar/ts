from .access import eligible_user_ids
from datetime import timedelta
from html import escape
import secrets
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from tender_search.client import ZakupClient, SourceError
from .models import AnnouncementWatch, StatusDelivery, TelegramSubscription, Favorite, Announcement
from .telegram_service import ALMATY, in_work_hours, telegram_api, TelegramError
from .source import RedisGate, source_client

STATUS_CHECK_INTERVAL = timedelta(minutes=15)


def cancel_watch(owner_id, announcement_id):
    with transaction.atomic():
        watches=list(AnnouncementWatch.objects.select_for_update().filter(owner_id=owner_id,announcement_id=announcement_id).select_related('announcement','lot'))
        for watch in watches:
            StatusDelivery.objects.filter(watch=watch,state='pending').update(state='cancelled')
            watch.enabled=False;watch.lease='';watch.lease_until=None
            watch.save(update_fields=['enabled','lease','lease_until','updated_at'])



def status_value(card):
    status=card.get('status') or {}
    if not isinstance(status,dict): status={'id':status,'name':card.get('status_name','')}
    name=status.get('name') or status.get('name_ru') or card.get('status_name','')
    identity=str(status.get('id') or status.get('code') or name)
    if not name or not identity: raise SourceError('В ответе портала отсутствует статус объявления.')
    return identity,name


def status_payload(watch, old_name):
    a,lot=watch.announcement,watch.lot
    esc=lambda value,length:escape(str(value or '')[:length])
    title=esc(a.name or lot.name,700)
    number=esc(a.number or lot.number,100)
    body=f'🔄 <b>Изменился статус объявления</b>\n<code>{number}</code>\n\n<b>{title}</b>\n\nБыло: {esc(old_name,300)}\nСтало: <b>{esc(watch.last_status_name,300)}</b>\n\n🕒 {timezone.now().astimezone(ALMATY).strftime("%d.%m.%Y %H:%M")} · Алматы'
    url=lot.portal_url
    return {'text':body,'parse_mode':'HTML','link_preview_options':{'is_disabled':True},'reply_markup':{'inline_keyboard':[[{'text':('Открыть на госзакупе ↗' if lot.announcement.source == 'zakup' else f'Открыть на {lot.portal_label} ↗'),'url':url}]]}}


def schedule_status_checks():
    if not settings.TELEGRAM_BOT_TOKEN: return
    from .tasks import check_announcement_status
    now=timezone.now()
    owners=TelegramSubscription.objects.filter(enabled=True,chat_id__isnull=False).values('owner_id')
    due = Q(next_check_at__isnull=True)|Q(next_check_at__lte=now)|Q(error='',checked_at__lte=now-STATUS_CHECK_INTERVAL)
    candidates=AnnouncementWatch.objects.filter(enabled=True,owner_id__in=owners).filter(owner_id__in=eligible_user_ids()).filter(due).filter(Q(lease_until__isnull=True)|Q(lease_until__lt=now))
    for watch in candidates.order_by('next_check_at','pk')[:100]:
        token=secrets.token_hex(16)
        claimed=candidates.filter(pk=watch.pk).update(lease=token,lease_until=now+timedelta(minutes=5))
        if claimed:
            try: check_announcement_status.delay(watch.pk,token)
            except Exception:
                AnnouncementWatch.objects.filter(pk=watch.pk,lease=token).update(lease='',lease_until=None,next_check_at=now+timedelta(minutes=5),error='Очередь недоступна.')


def check_status(watch_id, lease):
    watch=AnnouncementWatch.objects.filter(pk=watch_id,enabled=True,lease=lease,owner_id__in=eligible_user_ids()).select_related('announcement','lot').first()
    if not watch:return
    try:
        if not Favorite.objects.filter(owner=watch.owner,lot=watch.lot).exists():
            cancel_watch(watch.owner_id,watch.announcement_id);return
        if not TelegramSubscription.objects.filter(owner=watch.owner,enabled=True,chat_id__isnull=False).exists():return
        # Use only an authoritative announcement card, never the lot's list status.
        with (ZakupClient(gate=RedisGate()) if watch.announcement.source == 'zakup' else source_client(watch.announcement.source)) as api:card=api.announcement(watch.announcement.source_id)
        identity,name=status_value(card)
        with transaction.atomic():
            current=AnnouncementWatch.objects.select_for_update().select_related('announcement','lot').get(pk=watch_id)
            if not current.enabled or current.lease!=lease or current.generation!=watch.generation:return
            if not Favorite.objects.filter(owner=current.owner,lot=current.lot).exists():
                cancel_watch(current.owner_id,current.announcement_id);return
            changed=bool(current.last_status) and current.last_status!=identity
            old_name=current.last_status_name
            current.last_status,current.last_status_name=identity,name
            if changed:current.revision+=1
            current.checked_at=timezone.now()
            current.next_check_at=current.checked_at+STATUS_CHECK_INTERVAL
            current.error='';current.save()
            Announcement.objects.filter(pk=current.announcement_id).update(status=name,raw=card,checked_at=timezone.now())
            if changed:
                StatusDelivery.objects.filter(watch=current,state='pending').update(state='cancelled')
                key=f'{current.pk}:{current.generation}:{current.revision}'
                StatusDelivery.objects.get_or_create(event_key=key,defaults={'watch':current,'generation':current.generation,'revision':current.revision,'payload':status_payload(current,old_name)})
    except Exception as exc:
        AnnouncementWatch.objects.filter(pk=watch_id,lease=lease).update(error=str(exc)[:200] if isinstance(exc,SourceError) else 'Не удалось проверить статус. Повторим позже.',next_check_at=timezone.now()+timedelta(minutes=5))
    finally:
        AnnouncementWatch.objects.filter(pk=watch_id,lease=lease).update(lease='',lease_until=None)


def deliver_status_notifications():
    if not settings.TELEGRAM_BOT_TOKEN:return
    now=timezone.now()
    StatusDelivery.objects.filter(state='sending',updated_at__lt=now-timedelta(minutes=5)).update(state='uncertain',error='Доставка не подтверждена; повтор отключён.')
    deliveries=StatusDelivery.objects.filter(state='pending',watch__owner_id__in=eligible_user_ids()).filter(Q(retry_at__isnull=True)|Q(retry_at__lte=now)).select_related('watch')
    for delivery in deliveries[:20]:
        with transaction.atomic():
            watch=AnnouncementWatch.objects.select_for_update().get(pk=delivery.watch_id)
            current=StatusDelivery.objects.select_for_update().filter(pk=delivery.pk,state='pending').first()
            if not current:continue
            if (current.event_key.endswith((':start',':stop')) or current.generation!=watch.generation or current.revision!=watch.revision or
                    not watch.enabled or not Favorite.objects.filter(owner_id=watch.owner_id,lot_id=watch.lot_id).exists()):
                StatusDelivery.objects.filter(pk=current.pk).update(state='cancelled');continue
            # A committed in-flight send must finish before the next event for
            # this watch. Never hold a transaction open during the HTTP call.
            if StatusDelivery.objects.filter(watch=watch,state='sending').exists():continue
            sub=TelegramSubscription.objects.filter(owner_id=watch.owner_id,enabled=True,chat_id__isnull=False).first()
            if not sub:
                StatusDelivery.objects.filter(pk=current.pk).update(state='cancelled');continue
            StatusDelivery.objects.filter(pk=current.pk).update(state='sending',updated_at=timezone.now())
        try:
            result=telegram_api('sendMessage',dict(delivery.payload,chat_id=sub.chat_id))
            StatusDelivery.objects.filter(pk=delivery.pk).update(state='sent',message_id=result['message_id'],error='')
        except TelegramError as exc:
            if exc.code==429:
                StatusDelivery.objects.filter(pk=delivery.pk).update(state='pending',retry_at=timezone.now()+timedelta(seconds=max(1,exc.retry_after)),error='Лимит Telegram.')
                return
            if exc.code==403:
                TelegramSubscription.objects.filter(pk=sub.pk).update(enabled=False)
                from .workflow_notifications import cancel_pending
                cancel_pending(watch.owner_id)
            StatusDelivery.objects.filter(pk=delivery.pk).update(state='uncertain' if exc.code==0 else 'error',error=f'Telegram: {exc.code}')


