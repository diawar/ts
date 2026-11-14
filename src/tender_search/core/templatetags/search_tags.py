import re
from tender_search.matching import normalize, prepared_phrase
from django import template
from django.http import QueryDict
from django.utils.html import format_html_join, format_html

register = template.Library()


@register.inclusion_tag('core/_numbered_pagination.html', takes_context=True)
def numbered_pagination(context, page, anchor='', public=False):
    if not page or page.paginator.num_pages < 2:
        return {}
    request = context['request']
    def link(number):
        query = QueryDict(mutable=True) if public else request.GET.copy()
        # Deadline limits now belong to the saved search specification. Match
        # page-size/sort controls and discard this obsolete URL parameter.
        query.pop('remaining', None)
        if public and number == 1:
            query.pop('page', None)
        else:
            query['page'] = number
        if not public and context.get('per_page'):
            query['per_page'] = context['per_page']
        encoded = query.urlencode()
        return request.path + ('?' + encoded if encoded else '') + ('#' + anchor if anchor else '')
    items = [{'ellipsis': True} if number == page.paginator.ELLIPSIS else
             {'number': number, 'url': link(number), 'current': number == page.number}
             for number in page.paginator.get_elided_page_range(page.number, on_each_side=2, on_ends=1)]
    return {'items': items, 'public': public, 'previous': link(page.previous_page_number()) if page.has_previous() else '',
            'next': link(page.next_page_number()) if page.has_next() else ''}


@register.simple_tag(name='budget_unit_price')
def budget_unit_price_tag(lot):
    from tender_search.core.lot_pricing import budget_unit_price
    return budget_unit_price(lot)


@register.simple_tag
def lot_status(lot):
    watch=getattr(lot,'outcome_watch',None)
    if watch and watch.state in ('won','lost') and watch.protocol_url:
        return 'Итоги опубликованы'
    return lot.status or 'Не указан'


@register.simple_tag
def application_portal_status(lot):
    item=getattr(lot,'preparation',None)
    if not item:return ''
    title={'draft':'Черновик создан','submitted':'Заявка подана','completed':'Заявка завершена на портале'}.get(item.portal_state,'Данные подготовлены · черновик не подтверждён')
    if item.portal_sync_error:title+=' · требуется проверка'
    status=format_html('<span class="badge" title="По последнему отчёту расширения; актуальность проверяйте в карточке заявки">{}</span>',title)
    link=format_html(' <a href="{}" target="_blank" rel="noopener noreferrer">№{}</a>',item.portal_url,item.portal_number) if item.portal_number and item.portal_url else ''
    description=format_html(' <span class="badge">{}</span>','Описание товара заполнено') if item.portal_description_saved_at else ''
    return format_html('{}{}{}',status,link,description)


@register.simple_tag
def participation_warning(lot):
    # Unknown eligibility is not evidence of a restriction. Do not show an
    # alarming per-lot badge until a specific portal restriction is verified.
    if (lot.announcement.source != 'zakup' or
            getattr(lot, 'national_withdrawn', None) is not True or
            not getattr(lot, 'restriction_checked_at', None)):
        return ''
    return format_html('<span class="badge" style="background:#fff3cd;color:#664d03" title="{}">Изъятие из национального режима</span>',
        'Признак «Да» подтверждён в карточке этого лота. Допуск вашей организации проверяется отдельно на Госзакупе. Проверено: ' + str(lot.restriction_checked_at))


@register.simple_tag
def procurement_source_badge(lot, all_platforms=False):
    if lot.announcement.source in ('med_ecc', 'fms_ecc'):
        return format_html('<span class="source-badge" title="Медицинская площадка">{}</span>', lot.portal_label)
    raw = lot.raw or {}
    system = raw.get('system') or {}
    if not all_platforms and not system:
        system = (lot.announcement.raw or {}).get('system') or {}
    system_id = str((system.get('id') if isinstance(system, dict) else system) or raw.get('system_id') or '')
    if not all_platforms and system_id != '3':
        return ''
    labels = {'1': 'goszakup.gov.kz', '2': 'Mitwork', '3': 'Самрук-Казына',
              '4': 'EPZ', '6': 'SK Farm', '7': 'FMS'}
    label = labels.get(system_id) or (system.get('name') if isinstance(system, dict) else '')
    title = 'Площадка закупки' if label else 'Источник данных'
    label = label or 'goszakup.gov.kz'
    return format_html('<span class="source-badge" title="{}: {}">{}</span>', title, label, label)


@register.simple_tag
def deadline_remaining(deadline):
    from tender_search.core.deadlines import remaining_time
    from django.utils import timezone
    now = timezone.now()
    text, urgent = remaining_time(deadline, now=now)
    if not text:
        return ''
    return format_html('<span class="deadline-remaining{}" data-deadline="{}" data-deadline-now="{}">{}</span>',
                       ' deadline-remaining--urgent' if urgent else '', deadline.isoformat(), now.isoformat(), text)


@register.filter
def highlight(value, terms):
    text = str(value or "")
    terms = [t for t in terms if t]
    if not terms:
        return text
    spans=[]
    for term in terms:
        if len(term.split())>1:
            spans.extend(prepared_phrase(normalize(term)).spans(text))
        else:
            spans.extend(match.span() for match in re.finditer(r'(?<!\w)'+re.escape(term),text,re.IGNORECASE))
    merged=[]
    for start,end in sorted(set(spans)):
        if merged and start<merged[-1][1]:merged[-1]=(merged[-1][0],max(end,merged[-1][1]))
        else:merged.append((start,end))
    pieces=[];position=0
    for start,end in merged:
        pieces.extend([text[position:start],format_html('<mark>{}</mark>',text[start:end])]);position=end
    pieces.append(text[position:])
    return format_html_join('', '{}', ((piece,) for piece in pieces))


@register.simple_tag
def icon(name):
    from django.templatetags.static import static
    return format_html('<svg class="icon" aria-hidden="true" focusable="false"><use href="{}#{}"></use></svg>', static('core/icons.svg'), name)


@register.simple_tag
def search_conditions(spec):
    from tender_search.core.models import ReferenceEntry
    conditions = list(spec.get("terms", []))
    conditions.append("Любое слово — ИЛИ" if spec.get("mode","any")=="any" else "Все слова — И")
    names = {"contains":"Ключевое слово","word":"Целые слова","phrase":"Фразы","field":"Название целиком"}
    conditions.append(names.get(spec.get("kind","contains"),"Ключевое слово"))
    filters = spec.get("filters", {})
    for parameter, label in [("total_price__gte", "Сумма от"), ("total_price__lte", "Сумма до")]:
        if filters.get(parameter):
            conditions.append(f"{label}: {filters[parameter]} ₸")
    if spec.get("customer_bin"):
        conditions.append(f"БИН заказчика: {spec['customer_bin']}")
    for parameter, kind, label in [("status_id", "lot-statuses", "Статус"),
                                    ("purchase_method_id", "purchase-methods", "Способ закупки")]:
        value = filters.get(parameter + "__in", filters.get(parameter, ""))
        ids = str(value).split("__") if value else []
        names = {str(row.source_id): row.name for row in ReferenceEntry.objects.filter(kind=kind, source_id__in=[v for v in ids if v.isdigit()])}
        conditions.extend(f"{label}: {names.get(identifier, identifier)}" for identifier in ids)
    regions, cities = spec.get("regions", []), spec.get("cities", [])
    if regions or cities:
        for ids, kind, label in [(regions,"katos","Область"),(cities,"cities","Город")]:
            names = {str(row.source_id):row.name for row in ReferenceEntry.objects.filter(kind=kind,source_id__in=ids)}
            conditions.extend(f"{label}: {names.get(str(identifier), identifier)}" for identifier in ids)
    elif filters.get("delivery_address_id__in"):
        ids = str(filters["delivery_address_id__in"]).split("__")
        names = {str(row.source_id):row.name for row in ReferenceEntry.objects.filter(kind__in=["katos","cities"], source_id__in=[v for v in ids if v.isdigit()])}
        conditions.extend(f"Место поставки: {names.get(identifier,identifier)}" for identifier in ids)
    for parameter, label in [("offer_end_date__gte","Окончание приёма от"),("offer_end_date__lte","Окончание приёма до"),
                              ("lot_number","Номер лота"),("announcement_number","Номер объявления")]:
        if filters.get(parameter):
            value = str(filters[parameter])
            if parameter.startswith("offer_end_date"):
                parts = value[:10].split("-")
                if len(parts)==3:value=".".join(reversed(parts))
            if parameter.startswith("total_price"):value += " ₸"
            conditions.append(f"{label}: {value}")
    conditions.append("Автоматический обход страниц" if spec.get("auto_pages") else f"До {spec.get('depth', int(spec.get('pages',1))*50)} записей на слово")
    return conditions


@register.simple_tag
def condition_controls(spec):
    labels = search_conditions(spec)
    terms = spec.get("terms", [])
    controls = []
    prefixes = {"Сумма от:":"amount_min", "Сумма до:":"amount_max", "БИН заказчика:":"customer_bin",
                "Статус:":"status", "Способ закупки:":"method", "Область:":"region", "Город:":"city",
                "Место поставки:":"region", "Окончание приёма от:":"deadline_from", "Окончание приёма до:":"deadline_to",
                "Номер лота:":"number", "Номер объявления:":"number"}
    for index, label in enumerate(labels):
        if index < len(terms): field, value = "terms", terms[index]
        elif index == len(terms): field, value = "mode", "any"
        elif index == len(terms)+1: field, value = "kind", "contains"
        else:
            field = next((field for prefix, field in prefixes.items() if label.startswith(prefix)), "depth")
            value = label.split(": ",1)[1] if ": " in label else "500"
        controls.append({"label":label,"field":field,"value":value})
    controls.extend({"label":term,"field":"exclude","value":term} for term in spec.get("exclude",[]))
    return controls


@register.simple_tag
def lot_budget(lot):
    from tender_search.core.lot_pricing import lot_budget_display
    return lot_budget_display(lot)


@register.filter
def short_measure(value):
    name=str(value or '').strip()
    return {'штука':'шт.','штук':'шт.','шт':'шт.','шт.':'шт.','комплект':'компл.','набор':'набор','килограмм':'кг','грамм':'г','метр':'м','литр':'л','упаковка':'уп.','рулон':'рул.','пара':'пар.'}.get(name.casefold(),name)


@register.simple_tag
def draft_steps(item):
    from tender_search.core.draft_progress import steps_for
    return steps_for(item)
