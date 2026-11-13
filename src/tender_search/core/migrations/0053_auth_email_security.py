import uuid
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [('core', '0052_catalog_title_index'), migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [
        migrations.CreateModel(name='EmailVerification', fields=[
            ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('email', models.EmailField(max_length=254)), ('required', models.BooleanField(default=True)),
            ('nonce', models.CharField(default=uuid.uuid4, max_length=64)),
            ('verified_at', models.DateTimeField(blank=True, null=True)),
            ('owner', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='email_verification', to=settings.AUTH_USER_MODEL)),
        ]),
        migrations.CreateModel(name='AuthRateBucket', fields=[
            ('key', models.CharField(max_length=64, primary_key=True, serialize=False)),
            ('started_at', models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
            ('attempts', models.PositiveIntegerField(default=0)),
        ]),
        migrations.CreateModel(name='AuthMailJob', fields=[
            ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('kind', models.CharField(max_length=20)), ('email', models.EmailField(max_length=254)),
            ('origin', models.URLField()), ('nonce', models.CharField(blank=True, max_length=64)),
            ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
            ('available_at', models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
            ('completed_at', models.DateTimeField(db_index=True, null=True)),
            ('attempts', models.PositiveIntegerField(default=0)),
            ('last_error', models.CharField(blank=True, max_length=100)),
            ('owner', models.ForeignKey(null=True, on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL)),
        ]),
    ]
