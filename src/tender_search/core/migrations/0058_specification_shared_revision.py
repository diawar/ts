from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('core', '0057_funnel_status_and_launch_guards')]

    operations = [
        migrations.AddField(
            model_name='lotspecification',
            name='shared_revision',
            field=models.CharField(max_length=64, blank=True),
        ),
    ]
