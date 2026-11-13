"""Personal delivery state and owner-only acceptance documents."""
from pathlib import Path
from uuid import uuid4

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import LotShipment, UserLotWorkflow, SearchProfile


class ShipmentForm(forms.ModelForm):
    class Meta:
        model = LotShipment
        fields = ['mode', 'courier', 'tracking_number']
        labels = {'mode': 'Способ доставки', 'courier': 'Курьерская служба', 'tracking_number': 'Трек-номер'}

    def clean(self):
        data = super().clean()
        if data.get('mode') == 'courier':
            for field in ('courier', 'tracking_number'):
                if not data.get(field):
                    self.add_error(field, 'Заполните поле для курьерской доставки.')
        else:
            data['courier'] = data['tracking_number'] = ''
        return data


def context(owner, lot):
    state = lot.workflow if hasattr(lot, 'workflow') else UserLotWorkflow.objects.filter(owner=owner, lot=lot).first()
    shared = {'shared_acts': LotShipment.objects.filter(owner=owner,lot=lot).exclude(act='').order_by('-act_uploaded_at')}
    if not state or state.stage not in ('delivery', 'payment', 'won', 'archived'):
        return shared
    shipment = LotShipment.objects.filter(owner=owner, lot=lot).first()
    return {**shared, 'shipment': shipment, 'shipment_form': ShipmentForm(instance=shipment),
            'shipment_editable': state.stage == 'delivery'}


@login_required
@require_POST
def save(request, lot_id):
    with transaction.atomic():
        list(SearchProfile.objects.select_for_update().filter(owner=request.user).order_by('pk'))
        state = get_object_or_404(UserLotWorkflow.objects.select_for_update(), owner=request.user, lot_id=lot_id)
        if state.stage != 'delivery':
            messages.error(request, 'Доставка изменяется только на этапе «Поставка».')
            return redirect('lot_detail', lot_id=lot_id)
        shipment = LotShipment.objects.filter(owner=request.user, lot_id=lot_id).first()
        form = ShipmentForm(request.POST, instance=shipment)
        if form.is_valid():
            shipment = form.save(commit=False)
            shipment.owner = request.user
            shipment.lot_id = lot_id
            shipment.shipped_at = shipment.shipped_at or timezone.now()
            shipment.save()
            messages.success(request, 'Сведения об отправке сохранены.')
        else:
            messages.error(request, 'Укажите способ доставки, а для курьера — название службы и трек-номер.')
    return redirect('lot_detail', lot_id=lot_id)


@login_required
@require_POST
def upload_act(request, lot_id):
    from .fulfillment import accept_uploaded_act
    upload = request.FILES.get('act')
    suffix = Path(upload.name).suffix.lower() if upload else ''
    if not upload or suffix not in ('.pdf', '.jpg', '.jpeg', '.png') or upload.size > 20 * 1024 * 1024:
        messages.error(request, 'Прикрепите акт в PDF, JPG или PNG размером до 20 МБ.')
        return redirect('lot_detail', lot_id=lot_id)
    header = upload.read(8)
    upload.seek(0)
    valid = (suffix == '.pdf' and header.startswith(b'%PDF-')) or (suffix in ('.jpg', '.jpeg') and header.startswith(b'\xff\xd8\xff')) or (suffix == '.png' and header == b'\x89PNG\r\n\x1a\n')
    if not valid:
        messages.error(request, 'Содержимое файла не соответствует формату акта.')
        return redirect('lot_detail', lot_id=lot_id)
    with transaction.atomic():
        list(SearchProfile.objects.select_for_update().filter(owner=request.user).order_by('pk'))
        state = get_object_or_404(UserLotWorkflow.objects.select_for_update(), owner=request.user, lot_id=lot_id)
        shipment = LotShipment.objects.filter(owner=request.user, lot_id=lot_id).first()
        if state.stage != 'delivery' or not shipment or not shipment.shipped_at:
            messages.error(request, 'Сначала сохраните сведения об отправке на этапе «Поставка».')
            return redirect('lot_detail', lot_id=lot_id)
        old = shipment.act.name
        shipment.act.save(uuid4().hex + suffix, upload, save=False)
        shipment.act_uploaded_at = timezone.now()
        shipment.save()
        accept_uploaded_act(request.user.pk, lot_id)
        if old:
            storage = shipment.act.storage
            transaction.on_commit(lambda: storage.delete(old))
    messages.success(request, 'Акт сохранён. Лот перемещён в «Оплата».')
    return redirect('lot_detail', lot_id=lot_id)


@login_required
def download_act(request, lot_id):
    identifier=request.GET.get('act','')
    if not identifier.isdecimal() or len(identifier)>18:
        raise Http404
    shipment = get_object_or_404(LotShipment, pk=identifier, lot_id=lot_id,owner=request.user)
    if not shipment.act:
        raise Http404
    try:
        response=FileResponse(shipment.act.open('rb'), as_attachment=True, filename='act' + Path(shipment.act.name).suffix)
        response['Cache-Control']='private, no-store'
        return response
    except FileNotFoundError:
        raise Http404
