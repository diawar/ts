from django.conf import settings
from django.db import migrations, models
from django.db.models.functions import Lower, NullIf, Trim


def email_constraint():
    # Legacy accounts may have no address. NULL index keys allow multiple blanks.
    return models.UniqueConstraint(
        NullIf(Lower(Trim('email')), models.Value('')),
        name='unique_user_email_ci')


def install(apps, schema_editor):
    user = apps.get_model(settings.AUTH_USER_MODEL)
    schema_editor.add_constraint(user, email_constraint())


def uninstall(apps, schema_editor):
    user = apps.get_model(settings.AUTH_USER_MODEL)
    schema_editor.remove_constraint(user, email_constraint())


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0049_unique_company_bin'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    # The built-in auth model belongs to Django, so its index is database-only.
    operations = [migrations.RunPython(install, uninstall)]
