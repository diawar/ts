from django.db import migrations, models


STAGES = [('new', 'Сохранён в подборку'), ('preparing', 'В работе'),
          ('awaiting_results', 'Ожидаем итоги'), ('applied', 'Подали заявку'),
          ('won', 'Выиграли'), ('lost', 'Проиграли'), ('not_participated', 'Не участвовали'),
          ('contract', 'Заключение договора'), ('delivery', 'Поставка'),
          ('payment', 'Оплата'), ('archived', 'Архив')]


def merge_stages(apps, schema_editor):
    alias = schema_editor.connection.alias
    for name, fields in (
        ('CollectionLot', ('stage', 'expired_from_stage')),
        ('UserLotWorkflow', ('stage', 'expired_from_stage', 'archived_from_stage')),
    ):
        model = apps.get_model('core', name)
        for field in fields:
            model.objects.using(alias).filter(**{field: 'interesting'}).update(**{field: 'preparing'})
    # Preserve personal records, queue generations, prices and entry timestamps.


class Migration(migrations.Migration):
    dependencies = [('core', '0055_terminal_stage_archive')]
    operations = [
        migrations.RunPython(merge_stages, migrations.RunPython.noop),
        migrations.AlterField(model_name='collectionlot', name='stage',
            field=models.CharField(choices=STAGES, default='new', max_length=20)),
        migrations.AlterField(model_name='userlotworkflow', name='stage',
            field=models.CharField(choices=STAGES, default='new', max_length=20)),
        migrations.AlterField(model_name='userlotworkflow', name='archived_from_stage',
            field=models.CharField(blank=True, choices=STAGES, max_length=20)),
    ]
