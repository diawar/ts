from datetime import timedelta
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone
import tender_search.core.access


def existing_accounts(apps, schema_editor):
    users = apps.get_model(settings.AUTH_USER_MODEL)
    access = apps.get_model('core', 'TrialAccess')
    now = django.utils.timezone.now()
    alias = schema_editor.connection.alias
    access.objects.using(alias).bulk_create([
        access(owner_id=pk, started_at=now, expires_at=now+timedelta(days=14))
        for pk in users.objects.using(alias).filter(is_superuser=False).values_list('pk', flat=True)
    ])


class Migration(migrations.Migration):
    dependencies = [('core', '0050_unique_user_email'), migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [
        migrations.CreateModel(name='TrialAccess', fields=[
            ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('started_at', models.DateTimeField(default=django.utils.timezone.now)),
            ('expires_at', models.DateTimeField(db_index=True, default=tender_search.core.access.trial_end)),
            ('updated_at', models.DateTimeField(auto_now=True)),
            ('owner', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE,
                related_name='trial_access', to=settings.AUTH_USER_MODEL)),
        ]),
        migrations.RunPython(existing_accounts, migrations.RunPython.noop),
    ]
