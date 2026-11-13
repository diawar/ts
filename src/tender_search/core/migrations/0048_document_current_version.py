from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [('core', '0047_document_pending_projection_index')]
    operations = [
        migrations.AddField(
            model_name='document',
            name='current_version',
            field=models.ForeignKey(
                to='core.documentversion', null=True, blank=True,
                on_delete=django.db.models.deletion.SET_NULL, related_name='+'),
        ),
    ]
