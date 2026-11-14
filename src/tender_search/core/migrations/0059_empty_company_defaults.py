from django.db import migrations, models


DETAIL_LENGTHS = {
    'company_name': 500,
    'address_city': 200,
    'address_street': 300,
    'address_house': 50,
    'address_office': 50,
    'legal_address': 1000,
    'bank_account': 20,
    'bank_name': 200,
    'bank_bic': 11,
}


def clear_inherited_details(apps, schema_editor):
    # Run before AlterField: historical defaults identify the inherited details
    # without duplicating private installation data in another source file.
    Company = apps.get_model('core', 'CompanySettings')
    defaults = {name: Company._meta.get_field(name).get_default() for name in DETAIL_LENGTHS}
    alias = schema_editor.connection.alias
    candidates = Company.objects.using(alias).filter(
        company_bin='', bank_account=defaults['bank_account'])
    for company in candidates.iterator():
        updates = {name: '' for name, value in defaults.items()
                   if value and getattr(company, name) == value}
        if updates:
            # Keep custom details and all records with a filled BIN intact.
            Company.objects.using(alias).filter(pk=company.pk,
                company_bin='', **{name: defaults[name] for name in updates}).update(**updates)


class Migration(migrations.Migration):
    dependencies = [('core', '0058_specification_shared_revision')]

    operations = [
        migrations.RunPython(clear_inherited_details, migrations.RunPython.noop),
        *[
            migrations.AlterField(
                model_name='companysettings', name=name,
                field=models.CharField(max_length=length, blank=True, default=''),
            )
            for name, length in DETAIL_LENGTHS.items()
        ],
    ]
