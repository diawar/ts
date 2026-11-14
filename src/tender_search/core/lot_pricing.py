"""Personal bid calculator. Only explicit Goszakup identity and verified single-position cards."""
import re
from decimal import Decimal, InvalidOperation, localcontext, ROUND_HALF_UP
from django import forms
from .models import AnnouncementLotCache, UserLotPrice, CollectionLot

RULE_URL = 'https://wiki.goszakup.gov.kz/pages/viewpage.action?pageId=1311324'
CONFIRMED_METHODS = {11, 12, 23, 35, 36, 41}
CENT = Decimal('0.01')


def cached_pricing_detail(lot):
    """Reuse this request's card lookup, including a confirmed cache miss."""
    if hasattr(lot, '_cached_pricing_detail'):
        return lot._cached_pricing_detail
    detail = (lot.raw or {}).get('_pricing_detail')
    if not isinstance(detail, dict) or detail.get('id') != lot.source_id:
        if lot.announcement.source != 'zakup':
            return None
        cached = AnnouncementLotCache.objects.filter(pk=lot.announcement.source_id).only('rows').first()
        detail = next((item for item in (cached.rows if cached else [])
                       if isinstance(item, dict) and item.get('id') == lot.source_id), None)
    return detail


def budget_unit_price(lot):
    """Use only a verified single-position quantity already saved with this lot.

    No per-row requests or database lookups while rendering result tables.
    """
    detail=(lot.raw or {}).get('_pricing_detail')
    if not isinstance(detail,dict) or detail.get('id')!=lot.source_id or detail.get('is_combined') is not False:
        return None
    plans=detail.get('plan_items')
    if not isinstance(plans,list) or len(plans)!=1 or not isinstance(plans[0],dict):return None
    try:
        quantity=Decimal(str(plans[0].get('count')))
        amount=Decimal(str(lot.amount))
        if not quantity.is_finite() or not amount.is_finite() or not 0<quantity<=Decimal('1000000000000') or quantity.as_tuple().exponent < -6 or amount<0:return None
        with localcontext() as ctx:
            ctx.prec=50
            return (amount/quantity).quantize(CENT,rounding=ROUND_HALF_UP)
    except (InvalidOperation,TypeError,ValueError):
        return None


def pricing_detail_snapshot(detail):
    result = {key: detail.get(key) for key in ('id', 'system', 'is_combined')}
    plans = detail.get('plan_items')
    result['plan_items'] = [{key: plan.get(key) for key in ('id','external_id','count','measure','purchase_method','system','unit_price','total_price')} for plan in plans if isinstance(plan, dict)] if isinstance(plans, list) else None
    return result


def verified_pricing(lot):
    if lot.announcement.source != 'zakup':
        return None
    raw = lot.raw or {}
    try:
        method = int(raw.get('purchase_method_id'))
    except (ValueError, TypeError):
        return None
    if method not in CONFIRMED_METHODS:
        return None
    detail = cached_pricing_detail(lot)
    if not detail or detail.get('is_combined') is not False:
        return None
    plans = detail.get('plan_items')
    if not isinstance(plans, list) or len(plans) != 1 or not isinstance(plans[0], dict):
        return None
    identities = set()
    for data in (raw, detail, plans[0]):
        system = data.get('system')
        for value in (data.get('system_id'), system.get('id') if isinstance(system, dict) else system):
            if value is not None and value != '':
                identities.add(str(value))
    ann_system=(lot.announcement.raw or {}).get('system')
    ann_id=(lot.announcement.raw or {}).get('system_id') or (ann_system.get('id') if isinstance(ann_system,dict) else ann_system)
    if identities != {'1'} or (ann_id is not None and str(ann_id) not in ('1','4')):
        return None
    # Do not reuse a rule if the verified plan contradicts the registry method.
    plan_method = plans[0].get('purchase_method')
    if isinstance(plan_method, dict) and plan_method.get('id') is not None and str(plan_method['id']) != str(method):
        return None
    quantity = None
    try:
        quantity = Decimal(str(plans[0].get('count')))
        if not quantity.is_finite() or not 0 < quantity <= Decimal('1000000000000') or quantity.as_tuple().exponent < -6:
            quantity = None
    except InvalidOperation:
        pass
    measure = plans[0].get('measure') or {}
    return {'quantity': quantity, 'quantity_text': format(quantity, 'f') if quantity is not None else '', 'measure': measure.get('name_ru') or measure.get('name') or '' if isinstance(measure, dict) else str(measure), 'source_url': RULE_URL}


class UnitGrossField(forms.DecimalField):
    def to_python(self, value):
        if isinstance(value, str):
            value = re.sub(r'[\s\u00a0\u202f]', '', value).replace(',', '.')
            if value and not re.fullmatch(r'\d{1,20}(?:\.\d{0,2})?', value):
                raise forms.ValidationError('Введите положительную цену, не более двух знаков после запятой.')
        return super().to_python(value)


_COMPANY_UNSET = object()


def company_vat_rate(owner, *, company=_COMPANY_UNSET):
    if company is _COMPANY_UNSET:
        from .models import CompanySettings
        payer = CompanySettings.objects.filter(owner=owner).values_list('is_vat_payer', flat=True).first()
    else:
        payer = company.is_vat_payer if company else None
    return Decimal('0') if payer is False else Decimal('16')


def price_budget(lot):
    policy = verified_pricing(lot)
    if not policy or policy['quantity'] is None or lot.amount is None or lot.amount <= 0:
        return None
    quantity = policy['quantity']
    detail = cached_pricing_detail(lot) or {}
    try:
        unit = Decimal(str(detail['plan_items'][0].get('unit_price')))
        if not unit.is_finite() or unit <= 0: raise InvalidOperation
    except (InvalidOperation, KeyError, TypeError):
        with localcontext() as ctx:
            ctx.prec = 70
            unit = (lot.amount / quantity).quantize(CENT, rounding=ROUND_HALF_UP)
    return {'unit': unit, 'total': lot.amount, 'quantity': quantity}


class LotPriceForm(forms.Form):
    unit_gross = UnitGrossField(label='Наша цена за единицу с НДС, ₸', required=True,
        max_digits=22, decimal_places=2, min_value=Decimal('0.01'),
        widget=forms.TextInput(attrs={'inputmode':'decimal', 'autocomplete':'off', 'placeholder':'Например, 1160,00', 'maxlength':32}))
    confirm_discount = forms.BooleanField(required=False, label='Подтверждаю цену со снижением на 10% и более. Условия демпинга на портале проверены.')
    # POST data cannot change the company VAT rate.
    # Existing locked prices retain their recorded rate through form.initial.
    vat_rate = forms.DecimalField(initial=Decimal('16'), disabled=True, widget=forms.HiddenInput)

    def __init__(self, *args, lot=None, **kwargs):
        self.lot = lot
        self.budget = price_budget(lot) if lot is not None else None
        super().__init__(*args, **kwargs)

    def clean(self):
        data = super().clean()
        if 'unit_gross' not in data or 'vat_rate' not in data or self.lot is None:
            return data
        if not self.budget:
            self.add_error('unit_gross', 'Бюджет без НДС и количество не подтверждены. Сохранение цены недоступно.')
            return data
        result = calculate_price(data['unit_gross'], data['vat_rate'], self.budget['quantity'])
        if result['unit_net'] > self.budget['unit'] or result['total_net'] > self.budget['total']:
            self.add_error('unit_gross', 'Наша цена без НДС превышает бюджет за единицу или общую сумму лота.')
        elif result['total_net'] <= self.budget['total'] * Decimal('0.90') and not data.get('confirm_discount'):
            self.add_error('confirm_discount', 'Снижение на 10% и более: подтвердите цену перед сохранением.')
        return data


def calculate_price(gross, rate, quantity=None):
    with localcontext() as ctx:
        ctx.prec = 70
        money = lambda value: value.quantize(CENT, rounding=ROUND_HALF_UP)
        net = money(gross / (1 + rate / 100))
        result = {'unit_net':net, 'unit_vat':money(gross-net)}
        if quantity is not None:
            total_net = money(net * quantity)
            total_vat = money(total_net * rate / 100)
            total_gross = total_net + total_vat
            desired_total = money(gross * quantity)
            result.update(total_net=total_net, total_vat=total_vat, total_gross=total_gross,
                desired_total=desired_total, rounding_difference=money(total_gross-desired_total))
        return result


def price_entry_data(lot):
    if lot.announcement.source != 'zakup':
        return None
    detail = cached_pricing_detail(lot)
    if not detail or detail.get('is_combined') is not False:
        return None
    plans=detail.get('plan_items')
    if not isinstance(plans,list) or len(plans)!=1 or not isinstance(plans[0],dict):return None
    quantity=None
    try:
        value=Decimal(str(plans[0].get('count')))
        if value.is_finite() and 0<value<=Decimal('1000000000000') and value.as_tuple().exponent>=-6:quantity=value
    except InvalidOperation:pass
    return {'quantity':quantity,'quantity_text':format(quantity,'f') if quantity is not None else '',
            'measure':(plans[0].get('measure') or {}).get('name','') if isinstance(plans[0].get('measure'),dict) else '',
            'source_url':RULE_URL}


def pricing_context(owner, lot, form=None, *, company=_COMPANY_UNSET):
    entry=price_entry_data(lot)
    if entry is None:return {}
    saved=UserLotPrice.objects.filter(owner=owner,lot=lot).first()
    stages=set(CollectionLot.objects.filter(profile__owner=owner,lot=lot).values_list('stage',flat=True))
    interested=bool(stages & {'preparing'})
    if not interested and saved is None:return {}
    policy=verified_pricing(lot)
    entry['confirmed']=policy is not None
    if policy:entry.update(policy)
    locked=bool(saved and saved.locked_at)
    current_rate = saved.vat_rate if locked else company_vat_rate(owner,company=company)
    if form is None:
        form=LotPriceForm(lot=lot, initial={'unit_gross':format(saved.unit_gross,'.2f') if saved else '',
            'vat_rate':format(current_rate, 'f')})
    gross=saved.unit_gross if saved else None
    rate=current_rate
    if form.is_bound:
        gross=form.cleaned_data['unit_gross'] if form.is_valid() else None
        if form.is_valid():rate=form.cleaned_data['vat_rate']
    if rate == 0:
        form.fields['unit_gross'].label = 'Наша цена за единицу без НДС, ₸'
    result=calculate_price(gross,rate,entry['quantity']) if gross is not None else {}
    if gross is not None and entry['quantity'] is not None:
        with localcontext() as ctx:
            ctx.prec=70
            result['entered_total_gross']=(gross*entry['quantity']).quantize(CENT,rounding=ROUND_HALF_UP)
    editable=not locked and 'preparing' in stages
    if not editable:
        for field in form.fields.values():field.disabled=True
    can_cancel=locked and 'applied' in stages and not stages.intersection({'won','lost','contract','delivery','payment'})
    if can_cancel:
        from .models import ApplicationPreparation
        can_cancel=not ApplicationPreparation.objects.filter(owner=owner,lot=lot,portal_state__in=('submitted','completed')).exists()
    budget=price_budget(lot)
    return {'price_budget':budget,'price_editable':editable,'price_has_vat':rate > 0,'price_policy':entry,'price_form':form,'price_result':result,'price_locked':locked,'saved_price':saved,'price_can_cancel':can_cancel}


def combined_budget_display(lot, detail, plans):
    """Display a total only when distinct plan positions reconcile with the lot.

    The source can return multiple lifecycle versions of one external plan ID.
    Collapse those only if their quantity, measure and price agree. This is a
    display calculation; bid pricing/export rules remain single-position only.
    """
    unknown = {'multiple': True}
    if not plans or lot.amount is None:
        return unknown
    positions = {}
    try:
        for plan in plans:
            if not isinstance(plan, dict):
                return unknown
            count = Decimal(str(plan.get('count')))
            unit = Decimal(str(plan.get('unit_price')))
            if (not count.is_finite() or not 0 < count <= Decimal('1000000000000')
                    or count.as_tuple().exponent < -6 or not unit.is_finite() or unit < 0):
                return unknown
            measure = plan.get('measure') or {}
            measure_name = (measure.get('name_ru') or measure.get('name') or measure.get('short_name') or '') if isinstance(measure, dict) else str(measure)
            measure_key = str(measure.get('id') or measure_name.casefold()) if isinstance(measure, dict) else measure_name.casefold()
            if not measure_key:
                return unknown
            system = plan.get('system') or detail.get('system')
            system_id = str(system.get('id')) if isinstance(system, dict) else str(system or '')
            # Scope external IDs to their source system. Never guess identity
            # from a product name or silently choose conflicting plan versions.
            external_id = plan.get('external_id')
            if external_id and system_id:
                key = (system_id, 'external', str(external_id))
            elif plan.get('id'):
                key = ('plan', str(plan['id']))
            else:
                return unknown
            value = (count, unit, measure_key, measure_name)
            if key in positions and positions[key][:3] != value[:3]:
                return unknown
            positions[key] = value
        values = list(positions.values())
        if len({value[2] for value in values}) != 1:
            return unknown
        with localcontext() as ctx:
            ctx.prec = 70
            quantity = sum((value[0] for value in values), Decimal('0'))
            total = sum((value[0] * value[1] for value in values), Decimal('0'))
            if not lot.amount.is_finite() or total.quantize(CENT) != lot.amount.quantize(CENT):
                return unknown
        units = {value[1] for value in values}
        system = detail.get('system')
        system_id = system.get('id') if isinstance(system, dict) else system
        return {'quantity': quantity, 'unit': values[0][1] if len(units) == 1 else None,
                'measure': values[0][3], 'net': lot.announcement.source == 'zakup' and str(system_id) == '1',
                'multiple': True, 'positions': len(values), 'varied_units': len(units) > 1}
    except (InvalidOperation, KeyError, TypeError, ValueError):
        return unknown


def lot_budget_display(lot, detail=None):
    """Public quantity and unit budget, with no implicit per-row DB requests."""
    if hasattr(lot, '_budget_display') and detail is None:
        return lot._budget_display
    detail = detail if detail is not None else (lot.raw or {}).get('_pricing_detail')
    if not isinstance(detail, dict) or detail.get('id') != lot.source_id:
        return {}
    plans = detail.get('plan_items')
    if detail.get('is_combined') is True:
        return combined_budget_display(lot, detail, plans) if isinstance(plans, list) else {'multiple': True}
    if detail.get('is_combined') is not False:
        return {}
    if not isinstance(plans, list) or len(plans) != 1 or not isinstance(plans[0], dict):
        return {'multiple': True} if isinstance(plans, list) and len(plans) > 1 else {}
    plan = plans[0]
    try:
        quantity = Decimal(str(plan.get('count')))
        if not quantity.is_finite() or not 0 < quantity <= Decimal('1000000000000') or quantity.as_tuple().exponent < -6:
            return {}
    except (InvalidOperation, TypeError, ValueError):
        return {}
    unit = None
    try:
        value = Decimal(str(plan.get('unit_price')))
        if value.is_finite() and value >= 0: unit = value
    except (InvalidOperation, TypeError, ValueError):
        pass
    if unit is None and lot.amount is not None and lot.amount >= 0:
        with localcontext() as ctx:
            ctx.prec = 70
            unit = (lot.amount / quantity).quantize(CENT, rounding=ROUND_HALF_UP)
    measure = plan.get('measure') or {}
    system = detail.get('system')
    system = system.get('id') if isinstance(system, dict) else system
    net = lot.announcement.source == 'zakup' and str(system) == '1'
    return {'quantity': quantity, 'unit': unit, 'net': net,
        'measure': (measure.get('name_ru') or measure.get('name') or measure.get('short_name') or '') if isinstance(measure, dict) else str(measure)}


def attach_lot_budgets(lots):
    """Read cached card details for the entire displayed page in one query."""
    def needs_card(lot):
        if lot.announcement.source != 'zakup':
            return False
        detail = (lot.raw or {}).get('_pricing_detail')
        if not isinstance(detail, dict) or detail.get('id') != lot.source_id:
            return True
        # Older compact snapshots omitted external plan IDs. Prefer a cached
        # full card so lifecycle versions can be distinguished without IO.
        plans = detail.get('plan_items')
        return detail.get('is_combined') is True and isinstance(plans, list) and any(
            isinstance(plan, dict) and not plan.get('external_id') for plan in plans)
    missing = [lot for lot in lots if needs_card(lot)]
    cards = {}
    if missing:
        from django.db import connection
        from django.db.models.expressions import RawSQL
        wanted = {lot.source_id for lot in missing}
        caches = AnnouncementLotCache.objects.filter(pk__in={lot.announcement.source_id for lot in missing})
        if connection.vendor == 'postgresql':
            # Transfer only displayed positions, not the whole announcement JSON.
            caches = caches.annotate(display_rows=RawSQL(
                "(SELECT coalesce(jsonb_agg(item), '[]'::jsonb) FROM jsonb_array_elements("
                "CASE WHEN jsonb_typeof(rows) = 'array' THEN rows ELSE '[]'::jsonb END) item "
                "WHERE item->>'id' = ANY(%s))", [list(map(str, wanted))]))
            rows = caches.values_list('source_id', 'display_rows')
        else:
            rows = caches.values_list('source_id', 'rows')
        for announcement_id, details in rows:
            if isinstance(details, str):
                import json
                details = json.loads(details)
            for detail in details:
                if isinstance(detail, dict) and detail.get('id') in wanted:
                    cards[(announcement_id, detail['id'])] = detail
    for lot in lots:
        detail = cards.get((lot.announcement.source_id, lot.source_id)) if lot.announcement.source == 'zakup' else None
        raw_detail = (lot.raw or {}).get('_pricing_detail')
        lot._cached_pricing_detail = detail or (raw_detail if isinstance(raw_detail, dict) and raw_detail.get('id') == lot.source_id else None)
        lot._budget_display = lot_budget_display(lot, detail)
