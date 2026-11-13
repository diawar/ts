"""Read-only public catalog. Only source data; never personal lot/workflow data."""
from decimal import Decimal, InvalidOperation

from django.core.paginator import InvalidPage, Paginator
from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_safe

from .models import IndexedLot
from .catalog_sections import CATEGORIES, REGIONS, CatalogTitle, section_filter

CATALOG_TITLE = 'Открытый каталог тендеров Казахстана — TenderSearch'
CATALOG_DESCRIPTION = ('Закупки с открытым приёмом: заказчики, суммы, сроки и условия лотов. '
                       'Поиск, личные подборки, Telegram и CRM — 14 дней бесплатно после регистрации.')
CAPABILITIES = (
    ('search', 'Поиск по содержанию лота', 'Ищите по названию, описанию и доступному извлечённому тексту техспецификаций. Используйте ключевые слова, фразы и исключения.'),
    ('sliders-horizontal', 'Фильтры под вашу задачу', 'Отбирайте закупки по региону, бюджету, статусу и способу закупки. Быстрые фильтры по оставшимся часам помогают расставить приоритеты.'),
    ('folder', 'Личные подборки', 'Составляйте наборы ключевых слов для разных направлений. Сохраняйте условия и задавайте интервал фоновой проверки новых лотов.'),
    ('send', 'Telegram-уведомления', 'Получайте новые совпадения из подборок и изменения отслеживаемых закупок. Проверки подборок выполняются с 07:00 до 19:00 по Алматы.'),
    ('columns-3', 'Встроенная CRM', 'Ведите закупки от интересного лота и подготовки заявки до договора, поставки, оплаты и архива. Храните свои цены и заметки рядом с лотом.'),
    ('file-text', 'Подготовка заявки', 'Подготовьте сведения о товаре, производителе, происхождении и цене. Расширение браузера помогает перенести данные в черновик на Госзакупе.'),
    ('bell', 'Контроль итогов и договора', 'Отслеживайте итоги по БИН компании и сведения о договоре. Подтверждённые события помогают поддерживать актуальные этапы CRM.'),
    ('truck', 'Поставка, документы и оплата', 'Указывайте способ доставки и трек-номер, прикладывайте акт. После подтверждения полного исполнения и оплаты лот переходит в «Выиграли», а через 30 дней — в архив.'),
)


def public_lots():
    return IndexedLot.objects.filter(current=True, status_id__in=(7, 9), deadline__gt=timezone.now()).alias(catalog_title=CatalogTitle())


def label(value):
    if isinstance(value, dict):
        return value.get('full_name_ru') or value.get('full_name') or value.get('name_ru') or value.get('name') or ''
    return ''


def lot_card(lot):
    row = lot.row
    try:
        amount = Decimal(str(row.get('total_price')))
        if not amount.is_finite() or amount < 0 or amount.adjusted() > 20:
            amount = None
    except (InvalidOperation, ValueError, TypeError):
        amount = None
    announcement_id = lot.announcement_source_id
    # A fixed allowlist of public source fields, without forwarding raw JSON.
    return {
        'id': lot.pk, 'name': row.get('name_ru') or row.get('name') or 'Закупка без названия',
        'number': row.get('lot_number') or str(lot.pk),
        'description': row.get('description_ru') or '', 'amount': amount,
        'customers': [label(item) for item in row.get('customers') or [] if label(item)],
        'locations': [label(item) for item in row.get('delivery_addresses') or [] if label(item)],
        'method': row.get('purchase_method_name') or 'Способ не указан',
        'status': 'Приём заявок' if lot.status_id == 7 else 'Приём ценовых предложений',
        'deadline': lot.deadline, 'indexed_at': lot.indexed_at,
        'path': reverse('catalog_lot', args=[lot.pk]),
        'source_url': f'https://zakup.gov.kz/announcement/{announcement_id}#lot-{lot.pk}' if announcement_id else '',
    }


@require_safe
def catalog(request, category_slug=None, region_slug=None):
    section = None
    if category_slug is not None or region_slug is not None:
        sections, slug = (CATEGORIES, category_slug) if category_slug is not None else (REGIONS, region_slug)
        section = next((item for item in sections if item['slug'] == slug), None)
        if section is None:
            raise Http404('Раздел каталога не найден')
    lots = public_lots()
    if section:
        lots = lots.filter(section_filter(section))
    paginator = Paginator(lots.order_by('deadline', 'source_id').only(
        'source_id', 'announcement_source_id', 'row', 'deadline', 'status_id', 'indexed_at'), 24)
    # Invalid / empty pages are 404, not duplicate copies of page one.
    try:
        page = paginator.page(request.GET.get('page', '1'))
    except InvalidPage:
        raise Http404('Страница каталога не найдена')
    base_path = section['path'] if section else reverse('catalog')
    path = base_path + (f'?page={page.number}' if page.number > 1 else '')
    title = section['heading'] + ' — TenderSearch' if section else CATALOG_TITLE
    if page.number > 1:
        title = title.removesuffix(' — TenderSearch') + f' — страница {page.number} — TenderSearch'
    request.public_catalog_meta = {
        'title': title, 'description': section['description'] if section else CATALOG_DESCRIPTION,
        'path': path, 'name': section['name'] if section else 'Каталог закупок',
        'indexable': bool(paginator.count) if section else True,
    }
    return render(request, 'public/catalog.html', {
        'page': page, 'lots': [lot_card(lot) for lot in page], 'capabilities': CAPABILITIES,
        'section': section, 'categories': CATEGORIES, 'regions': REGIONS, 'catalog_path': base_path,
    })


@require_safe
def catalog_lot(request, source_id):
    card = lot_card(get_object_or_404(public_lots(), pk=source_id))
    request.public_catalog_meta = {
        'title': f"{card['name'][:120]} — лот {card['number']} — TenderSearch",
        'description': f"Закупка «{card['name'][:100]}»: сумма, заказчик, место поставки и срок подачи. Отслеживание и CRM — после регистрации в TenderSearch.",
        'path': card['path'], 'name': card['name'],
    }
    return render(request, 'public/catalog_lot.html', {'lot': card, 'capabilities': CAPABILITIES})
