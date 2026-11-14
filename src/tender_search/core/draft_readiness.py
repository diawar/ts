"""One gate for queue controls and server-side draft launch."""
from django.utils import timezone
from .preparation import PreparationForm,supports_preparation_export,draft_position_count
from .company_choices import TAXPAYER_TYPES


def readiness(item,company,preparation=None):
    busy=item.state in ('launching','running','waiting')
    reason=''
    if not company or not company.company_bin:
        reason='Укажите БИН и реквизиты организации в настройках.'
    elif not preparation:
        reason='Заполните товар и реквизиты в «Подготовке».'
    elif preparation.company_bin!=company.company_bin:
        reason='БИН изменён. Сохраните подготовку с актуальными реквизитами.'
    elif not PreparationForm(preparation.data).is_valid():
        reason='Заполните все обязательные данные товара и реквизиты в «Подготовке».'
    elif (preparation.data.get('taxpayer_type') or company.taxpayer_type) not in dict(TAXPAYER_TYPES) or not (preparation.data.get('taxpayer_type') or company.taxpayer_type):
        reason='Выберите тип налогоплательщика в настройках организации.'
    elif not supports_preparation_export(item.lot) or draft_position_count(item.lot) is None:
        reason='Площадка или состав лота не подтверждены.'
    valid_data=not reason
    if valid_data and item.lot.announcement.deadline and item.lot.announcement.deadline<=timezone.now():
        reason='Приём заявок завершён.'
    elif valid_data and getattr(item.lot,'workflow',None) and item.lot.workflow.stage!='preparing':
        reason='Лот уже перешёл на другой этап работы.'
    elif busy:
        reason='Сначала остановите заполнение и дождитесь подтверждения остановки.'
    elif item.state in ('complete','cancelled'):
        reason='Черновик уже готов.' if item.state=='complete' else 'Лот убран из очереди. Данные сохранены.'
    launch_ready=valid_data and not (item.lot.announcement.deadline and item.lot.announcement.deadline<=timezone.now()) and not (getattr(item.lot,'workflow',None) and item.lot.workflow.stage!='preparing')
    return {'launch_ready':bool(launch_ready),'input_ready':valid_data,'can_start':not reason,'can_edit':not busy,
        'can_remove':not busy,'reason':reason,
        'label':'Заполните данные' if item.state=='queued' and not valid_data else 'Ожидает запуска' if item.state=='queued' else item.get_state_display()}
