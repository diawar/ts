from django.conf import settings
from django.db import models
from django.utils import timezone
import uuid
from .access import trial_end


class EmailVerification(models.Model):
    owner = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='email_verification')
    email = models.EmailField(max_length=254)
    required = models.BooleanField(default=True)
    nonce = models.CharField(max_length=64, default=uuid.uuid4)
    verified_at = models.DateTimeField(null=True, blank=True)


class AuthRateBucket(models.Model):
    key = models.CharField(max_length=64, primary_key=True)
    started_at = models.DateTimeField(default=timezone.now, db_index=True)
    attempts = models.PositiveIntegerField(default=0)


class AuthMailJob(models.Model):
    kind = models.CharField(max_length=20)
    email = models.EmailField(max_length=254)
    origin = models.URLField()
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.CASCADE)
    nonce = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    available_at = models.DateTimeField(default=timezone.now, db_index=True)
    completed_at = models.DateTimeField(null=True, db_index=True)
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.CharField(max_length=100, blank=True)


class TrialAccess(models.Model):
    owner = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                related_name='trial_access')
    started_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField(default=trial_end, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)


class Timestamped(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class ProcurementCoverageSnapshot(models.Model):
    """Shared administrative counters, computed outside HTTP requests."""
    payload = models.JSONField(default=dict)
    measured_at = models.DateTimeField()


class Customer(Timestamped):
    source = models.CharField(max_length=40, default="zakup")
    source_id = models.BigIntegerField()
    bin = models.CharField(max_length=12, blank=True)
    name = models.TextField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["source", "source_id"], name="unique_customer_source")]

    def __str__(self):
        return self.name[:100]


class Announcement(Timestamped):
    source = models.CharField(max_length=40, default="zakup")
    source_id = models.BigIntegerField()
    external_id = models.BigIntegerField(null=True, blank=True)
    number = models.CharField(max_length=100, blank=True)
    name = models.TextField()
    status = models.CharField(max_length=150, blank=True)
    deadline = models.DateTimeField(null=True, blank=True, db_index=True)
    checked_at = models.DateTimeField(null=True, blank=True)
    raw = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["source", "source_id"], name="unique_announcement_source")]

    def __str__(self):
        return f"{self.number}: {self.name[:80]}"


class Lot(Timestamped):
    national_withdrawn = models.BooleanField(null=True, default=None)
    restriction_checked_at = models.DateTimeField(null=True, blank=True)
    restriction_source_url = models.URLField(blank=True)
    source_id = models.BigIntegerField()
    announcement = models.ForeignKey(Announcement, on_delete=models.CASCADE, related_name="lots")
    customer = models.ForeignKey(Customer, null=True, blank=True, on_delete=models.SET_NULL)
    external_id = models.BigIntegerField(null=True, blank=True)
    number = models.CharField(max_length=100, blank=True)
    name = models.TextField()
    description = models.TextField(blank=True)
    amount = models.DecimalField(max_digits=22, decimal_places=2, null=True, blank=True)
    status = models.CharField(max_length=150, blank=True)
    raw = models.JSONField(default=dict, blank=True)
    checked_at = models.DateTimeField(null=True, blank=True)
    refresh_started_at = models.DateTimeField(null=True, blank=True)
    refresh_state = models.CharField(max_length=20, blank=True)
    refresh_error = models.TextField(blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["announcement", "source_id"], name="unique_lot_source")]

    def __str__(self):
        return f"{self.number}: {self.name[:80]}"

    @property
    def portal_label(self):
        from tender_search.medical import HOSTS
        return HOSTS.get(self.announcement.source, 'zakup.gov.kz')

    @property
    def announcement_portal_url(self):
        from tender_search.medical import HOSTS
        if self.announcement.source in HOSTS:
            return self.portal_url
        identifier = self.announcement.external_id
        if not identifier:
            number = self.announcement.number.split('-', 1)[0]
            identifier = number if number.isdigit() else None
        return f'https://zakup.gov.kz/cabinet/announcement/{identifier}' if identifier else ''

    @property
    def portal_url(self):
        from tender_search.medical import HOSTS
        if self.announcement.source in HOSTS:
            return f'https://{self.portal_label}/ru/announce/index/{self.announcement.source_id}?tab=lots'
        return f'https://zakup.gov.kz/announcement/{self.announcement.source_id}#lot-{self.source_id}'


class TechnicalDocumentIndex(Timestamped):
    lot = models.OneToOneField(Lot, on_delete=models.CASCADE, related_name='technical_index')
    state = models.CharField(max_length=20, default='pending', db_index=True)
    checked_at = models.DateTimeField(null=True, blank=True)
    error = models.CharField(max_length=300, blank=True)


class Document(Timestamped):
    current_version = models.ForeignKey("DocumentVersion", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="+")
    kind = models.CharField(max_length=20, default='other', db_index=True)
    announcement = models.ForeignKey(Announcement, on_delete=models.CASCADE, related_name="documents")
    lot = models.ForeignKey(Lot, null=True, blank=True, on_delete=models.SET_NULL)
    source_id = models.CharField(max_length=100)
    name = models.TextField()
    url = models.URLField(max_length=2000)
    status = models.CharField(max_length=30, default="pending", db_index=True)
    error = models.TextField(blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["announcement", "source_id"], name="unique_document_source")]

    def __str__(self):
        return self.name[:100]


class DocumentVersion(Timestamped):
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="versions")
    sha256 = models.CharField(max_length=64, db_index=True)
    storage_path = models.CharField(max_length=500, blank=True)
    text = models.TextField(blank=True)
    search_text = models.TextField(null=True, editable=False)
    pages = models.JSONField(default=list, blank=True)
    used_ocr = models.BooleanField(default=False)

    def save(self, *args, **kwargs):
        fields = kwargs.get('update_fields')
        if fields is None or 'text' in fields:
            from tender_search.matching import normalize
            self.search_text = normalize(self.text)
            if fields is not None:
                kwargs['update_fields'] = set(fields) | {'search_text'}
        return super().save(*args, **kwargs)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["document", "sha256"], name="unique_document_version")]
        indexes = [models.Index(fields=['id'], condition=models.Q(search_text__isnull=True),
            name='idx_document_text_pending')]


class LotDocumentDownload(Timestamped):
    lot = models.OneToOneField(Lot, on_delete=models.CASCADE, related_name="document_download")
    state = models.CharField(max_length=20, default="queued")
    error = models.CharField(max_length=300, blank=True)


class AnnouncementLotCache(models.Model):
    source_id = models.BigIntegerField(primary_key=True)
    rows = models.JSONField(default=list)
    checked_at = models.DateTimeField(default=timezone.now, db_index=True)
    registry_fingerprint = models.CharField(max_length=64, blank=True)


class SearchProfile(Timestamped):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    name = models.CharField(max_length=200)
    include_terms = models.JSONField(default=list)
    exclude_terms = models.JSONField(default=list, blank=True)
    mode = models.CharField(max_length=3, choices=[("any", "ИЛИ"), ("all", "И")], default="any")
    filters = models.JSONField(default=dict, blank=True)
    enabled = models.BooleanField(default=False)
    interval_minutes = models.PositiveIntegerField(default=60)
    baseline_at = models.DateTimeField(null=True, blank=True)
    index_baseline_at = models.DateTimeField(null=True, blank=True)
    last_monitor_at = models.DateTimeField(null=True, blank=True)
    next_monitor_at = models.DateTimeField(null=True, blank=True)
    monitor_lease = models.CharField(max_length=64, blank=True)
    monitor_lease_until = models.DateTimeField(null=True, blank=True)
    monitor_error = models.TextField(blank=True)
    collection_enabled = models.BooleanField(default=True)
    collection_revision = models.PositiveIntegerField(default=1)
    collection_updated_at = models.DateTimeField(null=True, blank=True)
    collection_next_at = models.DateTimeField(null=True, blank=True)
    collection_lease = models.CharField(max_length=64, blank=True)
    collection_lease_until = models.DateTimeField(null=True, blank=True)
    collection_error = models.CharField(max_length=300, blank=True)

    @property
    def collection_hours(self):
        return min(12, max(1, (self.interval_minutes + 59)//60))

    @property
    def interval_hours(self):
        return str(min(12, max(1, (self.interval_minutes + 59)//60)))

    def __str__(self):
        return self.name


class CollectionLot(Timestamped):
    STAGES = [('new', 'Сохранён в подборку'), ('interesting', 'Интересные лоты'), ('preparing', 'Подготовка заявки'), ('awaiting_results', 'Ожидаем итоги'), ('applied', 'Подали заявку'),
              ('won', 'Выиграли'), ('lost', 'Проиграли'), ('not_participated', 'Не участвовали'), ('contract', 'Заключение договора'), ('delivery', 'Поставка'), ('payment', 'Оплата'), ('archived', 'Архив')]
    OUTCOMES = [('', 'Решение не указано'), ('won', 'Выиграли'), ('lost', 'Не выиграли')]
    profile = models.ForeignKey(SearchProfile, on_delete=models.CASCADE, related_name='collection_lots')
    lot = models.ForeignKey(Lot, on_delete=models.CASCADE)
    stage = models.CharField(max_length=20, choices=STAGES, default='new')
    outcome = models.CharField(max_length=10, choices=OUTCOMES, blank=True)
    expired_from_stage = models.CharField(max_length=20, blank=True)
    result_tracking = models.BooleanField(default=False)
    matches_filter = models.BooleanField(default=True)
    last_seen_at = models.DateTimeField(default=timezone.now)
    terms = models.JSONField(default=list)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['profile', 'lot'], name='unique_collection_lot')]
        indexes = [models.Index(fields=['profile', 'stage'], name='idx_collection_stage')]


class UserLotWorkflow(Timestamped):
    """Personal work survives removal of a saved filter; memberships are projections."""
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    lot = models.ForeignKey(Lot, on_delete=models.CASCADE)
    stage = models.CharField(max_length=20, choices=CollectionLot.STAGES, default='new')
    stage_entered_at = models.DateTimeField(default=timezone.now, db_index=True)
    outcome = models.CharField(max_length=10, choices=CollectionLot.OUTCOMES, blank=True)
    expired_from_stage = models.CharField(max_length=20, blank=True)
    result_tracking = models.BooleanField(default=False)
    # Manual return after deadline must not be undone by the next monitor tick.
    keep_after_deadline = models.BooleanField(default=False)
    archived_from_stage = models.CharField(max_length=20, choices=CollectionLot.STAGES, blank=True)
    archived_at = models.DateTimeField(null=True, blank=True)
    archived_result_tracking = models.BooleanField(default=False)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['owner', 'lot'], name='unique_user_lot_workflow')]


class ContractWatch(Timestamped):
    workflow = models.OneToOneField(UserLotWorkflow, on_delete=models.CASCADE)
    company_bin = models.CharField(max_length=12, blank=True)
    generation = models.CharField(max_length=64, default=uuid.uuid4)
    state = models.CharField(max_length=20, default='pending')
    evidence = models.JSONField(default=dict)
    error = models.CharField(max_length=300, blank=True)
    checked_at = models.DateTimeField(null=True, blank=True)
    next_check_at = models.DateTimeField(null=True, blank=True, db_index=True)
    lease = models.CharField(max_length=64, blank=True)
    lease_until = models.DateTimeField(null=True, blank=True)


class ApplicationPreparation(Timestamped):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    lot = models.ForeignKey(Lot, on_delete=models.CASCADE)
    company_bin = models.CharField(max_length=12)
    data = models.JSONField(default=dict)
    portal_state = models.CharField(max_length=20, default='unconfirmed')
    portal_number = models.CharField(max_length=100, blank=True)
    portal_url = models.URLField(max_length=500, blank=True)
    portal_checked_at = models.DateTimeField(null=True, blank=True)
    portal_confirmed_at = models.DateTimeField(null=True, blank=True)
    portal_sync_error = models.CharField(max_length=300, blank=True)
    portal_description_saved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['owner', 'lot'], name='unique_application_preparation')]


class DraftQueueItem(Timestamped):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    lot = models.ForeignKey(Lot, on_delete=models.CASCADE)
    generation = models.UUIDField(default=uuid.uuid4, editable=False)
    state = models.CharField(max_length=20, default='queued', choices=[
        ('queued','Ожидает'),('ready','Передано в расширение'),('running','Заполняется'),
        ('waiting','Ожидание восстановления'),('paused','Приостановлено'),('blocked','Требует проверки'),('complete','Черновик готов'),('cancelled','Убрано из очереди')])
    message = models.CharField(max_length=500, blank=True)
    progress_phase = models.CharField(max_length=20, blank=True)
    portal_check = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['owner','lot'], name='unique_draft_queue_lot')]


class UserLotNote(Timestamped):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    lot = models.ForeignKey(Lot, on_delete=models.CASCADE)
    text = models.TextField(blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['owner', 'lot'], name='unique_user_lot_note')]


class SearchRun(Timestamped):
    profile = models.ForeignKey(SearchProfile, on_delete=models.CASCADE, related_name="runs")
    status = models.CharField(max_length=30, default="pending")
    finished_at = models.DateTimeField(null=True, blank=True)
    complete = models.BooleanField(default=False)
    error = models.TextField(blank=True)


class Match(Timestamped):
    run = models.ForeignKey(SearchRun, on_delete=models.CASCADE)
    lot = models.ForeignKey(Lot, null=True, blank=True, on_delete=models.SET_NULL)
    version = models.ForeignKey(DocumentVersion, null=True, blank=True, on_delete=models.SET_NULL)
    term = models.TextField()
    snippet = models.TextField(blank=True)
    page_number = models.PositiveIntegerField(null=True, blank=True)


class Favorite(Timestamped):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    lot = models.ForeignKey(Lot, on_delete=models.CASCADE)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["owner", "lot"], name="unique_user_favorite")]


class Notification(Timestamped):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    dedup_key = models.CharField(max_length=200)
    message = models.TextField()
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["owner", "dedup_key"], name="unique_user_notification")]


class ReferenceEntry(models.Model):
    kind = models.CharField(max_length=40)
    source_id = models.BigIntegerField()
    name = models.CharField(max_length=500)
    raw = models.JSONField(default=dict)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["kind", "source_id"], name="unique_reference_entry")]
        ordering = ["name"]


class WebSearch(Timestamped):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    specification = models.JSONField()
    status = models.CharField(max_length=20, default="queued", db_index=True)
    scanned = models.PositiveIntegerField(default=0)
    source_counts = models.JSONField(default=dict)
    complete = models.BooleanField(default=False)
    error = models.TextField(blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)


class SearchHit(models.Model):
    search = models.ForeignKey(WebSearch, on_delete=models.CASCADE, related_name="hits")
    lot = models.ForeignKey(Lot, on_delete=models.CASCADE)
    terms = models.JSONField(default=list)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["search", "lot"], name="unique_search_hit")]


class SearchProjection(models.Model):
    search_text = models.TextField(null=True, editable=False)
    search_meta = models.JSONField(null=True, editable=False)
    search_fingerprint = models.CharField(max_length=64, null=True, editable=False)

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        fields = kwargs.get('update_fields')
        if fields is None or 'row' in fields:
            from .search_projection import projection
            try:
                self.search_text, self.search_meta, self.search_fingerprint = projection(self.row)
            except (TypeError, ValueError, AttributeError):
                self.search_text = self.search_meta = self.search_fingerprint = None
            if fields is not None:
                kwargs['update_fields'] = set(fields) | {'search_text', 'search_meta', 'search_fingerprint'}
        return super().save(*args, **kwargs)


class IndexedLot(SearchProjection):
    source_id = models.BigIntegerField(primary_key=True)
    announcement_source_id = models.BigIntegerField(null=True, db_index=True)
    registry_fingerprint = models.CharField(max_length=64, blank=True)
    row = models.JSONField()
    deadline = models.DateTimeField(db_index=True)
    status_id = models.PositiveIntegerField(db_index=True)
    method_id = models.PositiveIntegerField(db_index=True, null=True, blank=True)
    indexed_at = models.DateTimeField(default=timezone.now)
    seen_cycle = models.CharField(max_length=64, blank=True, db_index=True)
    current = models.BooleanField(default=True, db_index=True)
    details_skipped = models.BooleanField(default=False, db_index=True)

    class Meta:
        indexes = [models.Index(fields=['deadline'],
                               condition=models.Q(current=True, row___details_pending=True),
                               name='idx_pending_details_deadline')]


class ProcurementIndex(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1)
    cycle = models.CharField(max_length=64, blank=True)
    started_at = models.DateTimeField(null=True)
    completed_at = models.DateTimeField(null=True)
    heartbeat_at = models.DateTimeField(null=True)
    next_run_at = models.DateTimeField(null=True)
    pending = models.JSONField(default=list)
    source_count = models.PositiveIntegerField(default=0)
    processed = models.PositiveIntegerField(default=0)
    lease = models.CharField(max_length=64, blank=True)
    lease_until = models.DateTimeField(null=True)
    error = models.CharField(max_length=300, blank=True)
    paused = models.BooleanField(default=False)
    pipeline_version = models.PositiveSmallIntegerField(default=2)


class AnnouncementRefresh(models.Model):
    """Durable, coalescing detail queue; one task per source announcement."""
    source_id = models.BigIntegerField(primary_key=True)
    announcement = models.JSONField(default=dict, blank=True)
    registry_fingerprint = models.CharField(max_length=64, blank=True)
    seen_cycle = models.CharField(max_length=64, blank=True, db_index=True)
    active = models.BooleanField(default=True)
    deadline = models.DateTimeField()
    revision = models.PositiveIntegerField(default=1)
    urgent = models.BooleanField(default=True)
    due_at = models.DateTimeField(default=timezone.now)
    checked_at = models.DateTimeField(null=True)
    attempts = models.PositiveIntegerField(default=0)
    error = models.CharField(max_length=300, blank=True)
    lease = models.CharField(max_length=64, blank=True)
    lease_until = models.DateTimeField(null=True)

    class Meta:
        indexes = [models.Index(fields=['due_at'], condition=models.Q(active=True),
                               name='idx_announcement_refresh_due')]


class MedicalIndexedLot(SearchProjection):
    source = models.CharField(max_length=40)
    source_id = models.BigIntegerField()
    row = models.JSONField()
    deadline = models.DateTimeField(db_index=True)
    status_id = models.PositiveIntegerField(db_index=True)
    method_id = models.PositiveIntegerField(db_index=True, null=True)
    indexed_at = models.DateTimeField(default=timezone.now)
    seen_cycle = models.CharField(max_length=64, db_index=True)
    current = models.BooleanField(default=True, db_index=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['source', 'source_id'], name='unique_medical_index_lot')]


class MedicalIndex(models.Model):
    source = models.CharField(max_length=40, primary_key=True)
    cycle = models.CharField(max_length=64, blank=True)
    cursor = models.JSONField(default=dict)
    completed_at = models.DateTimeField(null=True)
    heartbeat_at = models.DateTimeField(null=True)
    next_run_at = models.DateTimeField(null=True)
    lease = models.CharField(max_length=64, blank=True)
    lease_until = models.DateTimeField(null=True)
    error = models.CharField(max_length=300, blank=True)


class SearchIndexEpoch(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1)
    revision = models.BigIntegerField(default=0)


class SearchCandidateCache(models.Model):
    key = models.CharField(primary_key=True, max_length=64)
    revision = models.BigIntegerField()
    candidates = models.JSONField()
    expires_at = models.DateTimeField(db_index=True)


class LeadCompany(Timestamped):
    STATUS_CHOICES = [('new', 'Новый'), ('contacted', 'Связались'), ('client', 'Клиент'), ('excluded', 'Не связываться')]
    identifier = models.CharField(max_length=12, unique=True)
    identifier_kind = models.CharField(max_length=3, blank=True)
    name = models.TextField()
    registry_id = models.PositiveBigIntegerField(null=True, blank=True)
    registry_url = models.URLField(max_length=500, blank=True)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default='new', db_index=True)
    notes = models.TextField(blank=True)
    last_seen_at = models.DateTimeField(default=timezone.now)
    contact_state = models.CharField(max_length=12, default='pending', db_index=True,
        choices=[('pending', 'Ожидает проверки'), ('ready', 'E-mail найден'), ('missing', 'E-mail не опубликован'), ('error', 'Ошибка проверки')])
    contact_checked_at = models.DateTimeField(null=True, blank=True)
    next_contact_check_at = models.DateTimeField(default=timezone.now, db_index=True)
    contact_attempts = models.PositiveIntegerField(default=0)
    contact_error = models.CharField(max_length=300, blank=True)

    def __str__(self):
        return f'{self.identifier} — {self.name[:100]}'


class LeadEmail(Timestamped):
    company = models.ForeignKey(LeadCompany, on_delete=models.CASCADE, related_name='emails')
    email = models.EmailField(max_length=254)
    source_url = models.URLField(max_length=500)
    verified_at = models.DateTimeField(default=timezone.now)
    current = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['company', 'email'], name='unique_company_lead_email')]


class LeadProcurement(Timestamped):
    source_id = models.PositiveBigIntegerField(unique=True)
    external_id = models.PositiveBigIntegerField(db_index=True)
    number = models.CharField(max_length=100)
    name = models.TextField()
    method_name = models.CharField(max_length=300, blank=True)
    results_published_at = models.DateTimeField(db_index=True)
    state = models.CharField(max_length=12, default='queued', db_index=True,
        choices=[('queued', 'В очереди'), ('processing', 'Обрабатывается'), ('done', 'Обработан'), ('retry', 'Повторная попытка'), ('review', 'Требует проверки')])
    protocol_url = models.URLField(max_length=500, blank=True)
    participants_count = models.PositiveIntegerField(default=0)
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now, db_index=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    error = models.CharField(max_length=300, blank=True)

    @property
    def source_url(self):
        return f'https://old.goszakup.gov.kz/ru/announce/index/{self.external_id}?tab=protocols'


class LeadParticipation(Timestamped):
    company = models.ForeignKey(LeadCompany, on_delete=models.CASCADE, related_name='participations')
    procurement = models.ForeignKey(LeadProcurement, on_delete=models.CASCADE, related_name='participations')
    supplier_name = models.TextField()
    lot_numbers = models.JSONField(default=list)
    winner = models.BooleanField(default=False)
    protocol_url = models.URLField(max_length=500)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['company', 'procurement'], name='unique_lead_participation')]


class LeadCollectionState(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1)
    enabled = models.BooleanField(default=False)
    lookback_days = models.PositiveSmallIntegerField(default=30)
    windows = models.JSONField(default=list)
    started_at = models.DateTimeField(null=True)
    completed_at = models.DateTimeField(null=True)
    next_scan_at = models.DateTimeField(null=True)
    heartbeat_at = models.DateTimeField(null=True)
    lease = models.CharField(max_length=64, blank=True)
    lease_until = models.DateTimeField(null=True)
    error = models.CharField(max_length=300, blank=True)


class TelegramSubscription(Timestamped):
    owner = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    chat_id = models.BigIntegerField(null=True, blank=True, unique=True)
    enabled = models.BooleanField(default=False)
    link_hash = models.CharField(max_length=64, blank=True, db_index=True)
    link_expires_at = models.DateTimeField(null=True, blank=True)

class TelegramSeen(models.Model):
    profile = models.ForeignKey(SearchProfile, on_delete=models.CASCADE)
    announcement_key = models.CharField(max_length=64)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["profile", "announcement_key"], name="unique_profile_seen_announcement")]

class TelegramDelivery(Timestamped):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    profile = models.ForeignKey(SearchProfile, on_delete=models.CASCADE)
    announcement_key = models.CharField(max_length=64)
    lot = models.ForeignKey(Lot, on_delete=models.CASCADE)
    state = models.CharField(max_length=20, default="pending")
    error = models.CharField(max_length=200, blank=True)
    message_id = models.BigIntegerField(null=True, blank=True)
    interest_token = models.UUIDField(null=True, blank=True, editable=False)
    retry_at = models.DateTimeField(null=True, blank=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["owner", "announcement_key"], name="unique_owner_telegram_announcement")]

class TelegramCursor(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1)
    offset = models.BigIntegerField(default=0)


class AnnouncementWatch(Timestamped):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    announcement = models.ForeignKey(Announcement, on_delete=models.CASCADE)
    lot = models.ForeignKey(Lot, on_delete=models.CASCADE)
    enabled = models.BooleanField(default=False)
    generation = models.CharField(max_length=64, blank=True)
    last_status = models.CharField(max_length=200, blank=True)
    last_status_name = models.CharField(max_length=300, blank=True)
    revision = models.PositiveIntegerField(default=0)
    checked_at = models.DateTimeField(null=True, blank=True)
    next_check_at = models.DateTimeField(null=True, blank=True)
    lease = models.CharField(max_length=64, blank=True)
    lease_until = models.DateTimeField(null=True, blank=True)
    error = models.CharField(max_length=200, blank=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["owner", "announcement"], name="unique_owner_announcement_watch")]

class StatusDelivery(Timestamped):
    watch = models.ForeignKey(AnnouncementWatch, on_delete=models.CASCADE)
    generation = models.CharField(max_length=64)
    event_key = models.CharField(max_length=150, unique=True)
    payload = models.JSONField(default=dict)
    state = models.CharField(max_length=20, default="pending")
    error = models.CharField(max_length=200, blank=True)
    retry_at = models.DateTimeField(null=True, blank=True)
    message_id = models.BigIntegerField(null=True, blank=True)


class TelegramContact(models.Model):
    chat_id = models.BigIntegerField(primary_key=True)
    username = models.CharField(max_length=64, blank=True)
    first_name = models.CharField(max_length=255, blank=True)
    last_name = models.CharField(max_length=255, blank=True)
    enabled = models.BooleanField(default=True)
    first_seen_at = models.DateTimeField(null=True, blank=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)


class UserLotPrice(Timestamped):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    lot = models.ForeignKey(Lot, on_delete=models.CASCADE)
    unit_gross = models.DecimalField(max_digits=22, decimal_places=2)
    locked_at = models.DateTimeField(null=True, blank=True)
    vat_rate = models.DecimalField(max_digits=5, decimal_places=2, default=16)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['owner', 'lot'], name='unique_user_lot_price'),
            models.CheckConstraint(condition=models.Q(unit_gross__gt=0), name='positive_user_lot_price'),
            models.CheckConstraint(condition=models.Q(vat_rate__in=[0,5,12,16]), name='supported_user_vat_rate')]


class CompanySettings(Timestamped):
    is_vat_payer = models.BooleanField(default=True)
    owner = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    company_bin = models.CharField(max_length=12, blank=True, default="")
    company_name = models.CharField(max_length=500, blank=True, default="ТОО \"OVERSHOP.KZ\"")
    address_city = models.CharField(max_length=200, blank=True, default="Павлодар")
    address_street = models.CharField(max_length=300, blank=True, default="Желтоксан")
    address_house = models.CharField(max_length=50, blank=True, default="7")
    address_office = models.CharField(max_length=50, blank=True, default="25Б")
    legal_address = models.CharField(max_length=1000, blank=True, default="Павлодар, Желтоксан, 7, 25Б")
    bank_account = models.CharField(max_length=20, blank=True, default="KZ92601A241008980281")
    bank_name = models.CharField(max_length=200, blank=True, default="АО \"Народный Банк Казахстана\"")
    bank_bic = models.CharField(max_length=11, blank=True, default="HSBKKZKX")
    contact_phone = models.CharField(max_length=25, blank=True)
    taxpayer_type = models.CharField(max_length=200, blank=True, default="Юридическое лицо")

    class Meta:
        constraints = [models.UniqueConstraint(fields=['company_bin'],
            condition=~models.Q(company_bin=''), name='unique_company_bin')]


class LotShipment(Timestamped):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    lot = models.ForeignKey(Lot, on_delete=models.CASCADE)
    mode = models.CharField(max_length=16, choices=[('courier', 'Курьерская служба'), ('own', 'Своими силами')])
    courier = models.CharField(max_length=200, blank=True)
    tracking_number = models.CharField(max_length=200, blank=True)
    shipped_at = models.DateTimeField(null=True, blank=True)
    act = models.FileField(upload_to='private-acts/%Y/%m/', blank=True)
    act_uploaded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['owner', 'lot'], name='unique_owner_lot_shipment')]


class LotOutcomeWatch(Timestamped):
    STATES = [('pending','Ожидаем публикации итогов'), ('won','Выиграли'), ('lost','Не выиграли'),
              ('review','Итоги требуют проверки'), ('not_listed','БИН не найден среди участников'),
              ('cancelled','Закупка отменена'), ('unsuccessful','Закупка не состоялась'),
              ('unsupported','Источник итогов не подтверждён'), ('no_bin','Укажите БИН компании')]
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    lot = models.ForeignKey(Lot, on_delete=models.CASCADE)
    enabled = models.BooleanField(default=True)
    company_bin = models.CharField(max_length=12, blank=True)
    generation = models.CharField(max_length=64, default=uuid.uuid4)
    state = models.CharField(max_length=20, choices=STATES, default='pending')
    winner_bin = models.CharField(max_length=12, blank=True)
    winner_name = models.CharField(max_length=500, blank=True)
    protocol_url = models.URLField(max_length=500, blank=True)
    fingerprint = models.CharField(max_length=64, blank=True)
    revision = models.PositiveIntegerField(default=0)
    applied_at = models.DateTimeField(default=timezone.now)
    checked_at = models.DateTimeField(null=True, blank=True)
    next_check_at = models.DateTimeField(null=True, blank=True, db_index=True)
    lease = models.CharField(max_length=64, blank=True)
    lease_until = models.DateTimeField(null=True, blank=True)
    error = models.CharField(max_length=300, blank=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=['owner','lot'], name='unique_owner_lot_outcome')]


class OutcomeDelivery(Timestamped):
    watch = models.ForeignKey(LotOutcomeWatch, on_delete=models.CASCADE)
    generation = models.CharField(max_length=64)
    revision = models.PositiveIntegerField(default=0)
    event_key = models.CharField(max_length=150, unique=True)
    payload = models.JSONField(default=dict)
    state = models.CharField(max_length=20, default='pending')
    message_id = models.BigIntegerField(null=True, blank=True)
    retry_at = models.DateTimeField(null=True, blank=True)
    error = models.CharField(max_length=200, blank=True)


class WorkflowDelivery(Timestamped):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    lot = models.ForeignKey(Lot, on_delete=models.CASCADE)
    company_bin = models.CharField(max_length=12)
    kind = models.CharField(max_length=20)
    event_key = models.CharField(max_length=64, unique=True)
    payload = models.JSONField(default=dict)
    state = models.CharField(max_length=20, default='pending', db_index=True)
    retry_at = models.DateTimeField(null=True, blank=True)
    message_id = models.BigIntegerField(null=True, blank=True)
    error = models.CharField(max_length=200, blank=True)


class LotSpecification(Timestamped):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    lot = models.ForeignKey(Lot, on_delete=models.CASCADE)
    state = models.CharField(max_length=20, default='queued')
    generation = models.CharField(max_length=64, blank=True)
    documents = models.JSONField(default=list)
    error = models.CharField(max_length=300, blank=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=['owner','lot'], name='unique_owner_lot_specification')]


class TechnicalIndexProgress(models.Model):
    started_at = models.DateTimeField()
    completed_at = models.DateTimeField(null=True, blank=True)
    targets = models.JSONField(default=list)
    counts = models.JSONField(default=dict)
    updated_at = models.DateTimeField(auto_now=True)
