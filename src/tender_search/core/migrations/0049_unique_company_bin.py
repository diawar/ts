from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('core', '0048_document_current_version')]
    operations = [
        migrations.AlterField(model_name='companysettings', name='company_bin',
            field=models.CharField(max_length=12, blank=True, default='')),
        migrations.AddConstraint(model_name='companysettings',
            constraint=models.UniqueConstraint(fields=['company_bin'],
                condition=~models.Q(company_bin=''), name='unique_company_bin')),
    ]
