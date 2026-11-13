from django.db import migrations


def install(apps, schema_editor):
    if schema_editor.connection.vendor == 'postgresql':
        # Concurrent creation keeps the background ingestion worker writable.
        schema_editor.execute('CREATE EXTENSION IF NOT EXISTS pg_trgm')
        schema_editor.execute("""CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_catalog_title_trgm
            ON core_indexedlot USING gin
            ((COALESCE(NULLIF(row->>'name_ru', ''), row->>'name', '')) gin_trgm_ops)
            WHERE current""")


def uninstall(apps, schema_editor):
    if schema_editor.connection.vendor == 'postgresql':
        schema_editor.execute('DROP INDEX CONCURRENTLY IF EXISTS idx_catalog_title_trgm')


class Migration(migrations.Migration):
    atomic = False
    dependencies = [('core', '0051_trial_access')]
    operations = [migrations.RunPython(install, uninstall)]
