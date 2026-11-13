from datetime import timedelta

from django.db import migrations, models
from django.db.models import F
from django.utils import timezone


def reconcile(apps, schema_editor):
    Workflow = apps.get_model('core', 'UserLotWorkflow')
    Collection = apps.get_model('core', 'CollectionLot')
    Favorite = apps.get_model('core', 'Favorite')
    # Earlier versions did not record transitions. Use the last known update
    # conservatively, rather than guessing an earlier date for old terminal rows.
    Workflow.objects.update(stage_entered_at=F('updated_at'))
    for state in Workflow.objects.filter(stage='won', completed_at__isnull=True).iterator():
        Workflow.objects.filter(pk=state.pk).update(stage='contract')
        Collection.objects.filter(profile__owner_id=state.owner_id, lot_id=state.lot_id).update(stage='contract')
    Workflow.objects.filter(stage='archived', archived_from_stage='won',
                            completed_at__isnull=True).update(archived_from_stage='contract')
    cutoff = timezone.now() - timedelta(days=30)
    # Previously a paid deal went straight to archive. Keep recent completions
    # visible in Won for the remainder of their 30 days; retain older history.
    for state in Workflow.objects.filter(stage='archived', archived_from_stage='payment',
                                          completed_at__isnull=False).iterator():
        values = {'archived_from_stage': 'won', 'archived_result_tracking': False}
        if state.completed_at > cutoff:
            values.update(stage='won', result_tracking=False, stage_entered_at=state.completed_at,
                          archived_at=None, archived_from_stage='')
            Collection.objects.filter(profile__owner_id=state.owner_id, lot_id=state.lot_id).update(
                stage='won', result_tracking=False)
            Favorite.objects.get_or_create(owner_id=state.owner_id, lot_id=state.lot_id)
        Workflow.objects.filter(pk=state.pk).update(**values)


class Migration(migrations.Migration):
    dependencies = [('core', '0054_awaiting_results_stage')]
    operations = [
        migrations.AddField(model_name='userlotworkflow', name='stage_entered_at',
                            field=models.DateTimeField(db_index=True, default=timezone.now)),
        migrations.RunPython(reconcile, migrations.RunPython.noop),
    ]
