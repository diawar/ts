from django.db import migrations, models


STAGES = [('new', 'Сохранён в подборку'), ('interesting', 'Интересные лоты'),
          ('preparing', 'Подготовка заявки'), ('awaiting_results', 'Ожидаем итоги'),
          ('applied', 'Подали заявку'), ('won', 'Выиграли'), ('lost', 'Проиграли'),
          ('not_participated', 'Не участвовали'), ('contract', 'Заключение договора'),
          ('delivery', 'Поставка'), ('payment', 'Оплата'), ('archived', 'Архив')]


def reconcile(apps, schema_editor):
    Workflow=apps.get_model('core','UserLotWorkflow')
    Preparation=apps.get_model('core','ApplicationPreparation')
    Company=apps.get_model('core','CompanySettings')
    Profile=apps.get_model('core','SearchProfile')
    Collection=apps.get_model('core','CollectionLot')
    Queue=apps.get_model('core','DraftQueueItem')
    for state in Workflow.objects.filter(stage='preparing').iterator():
        saved=Preparation.objects.filter(owner_id=state.owner_id,lot_id=state.lot_id,
            portal_state='draft',portal_description_saved_at__isnull=False,
            portal_checked_at__isnull=False,portal_confirmed_at__isnull=False).exclude(portal_number='').exclude(company_bin='').first()
        if not saved or not Company.objects.select_for_update().filter(owner_id=state.owner_id,company_bin=saved.company_bin).first():
            continue
        list(Profile.objects.select_for_update().filter(owner_id=state.owner_id).order_by('pk'))
        values={'stage':'awaiting_results','result_tracking':False,'expired_from_stage':''}
        if not Workflow.objects.filter(pk=state.pk,stage='preparing').update(**values):
            continue
        Collection.objects.filter(profile__owner_id=state.owner_id,lot_id=state.lot_id).update(**values)
        Queue.objects.filter(owner_id=state.owner_id,lot_id=state.lot_id).exclude(state='cancelled').update(state='complete')


def reverse(apps, schema_editor):
    for name in ('UserLotWorkflow','CollectionLot'):
        apps.get_model('core',name).objects.filter(stage='awaiting_results').update(stage='preparing')


class Migration(migrations.Migration):
    dependencies=[('core','0053_auth_email_security')]
    operations=[
        migrations.AlterField(model_name='collectionlot',name='stage',field=models.CharField(choices=STAGES,default='new',max_length=20)),
        migrations.AlterField(model_name='userlotworkflow',name='stage',field=models.CharField(choices=STAGES,default='new',max_length=20)),
        migrations.AlterField(model_name='userlotworkflow',name='archived_from_stage',field=models.CharField(blank=True,choices=STAGES,max_length=20)),
        migrations.RunPython(reconcile,reverse),
    ]
