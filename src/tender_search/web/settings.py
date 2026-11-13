import os
from pathlib import Path

BASE_DIR = Path(os.getenv("TENDER_BASE_DIR", Path(__file__).resolve().parents[3]))
SECRET_KEY = os.environ["DJANGO_SECRET_KEY"]
DEBUG = os.getenv("DJANGO_DEBUG", "0") == "1"
ALLOWED_HOSTS = os.getenv("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]").split(",")
PUBLIC_SITE_URL = os.getenv("PUBLIC_SITE_URL", "")
AUTH_SITE_URL = os.getenv("AUTH_SITE_URL", "http://localhost:8001")
AUTH_TRUST_CLOUDFLARE_IP = os.getenv("AUTH_TRUST_CLOUDFLARE_IP", "0") == "1"
PASSWORD_RESET_TIMEOUT = 3600
EMAIL_VERIFICATION_TIMEOUT = 86400
EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST = os.getenv('EMAIL_HOST', 'mailpit')
EMAIL_PORT = int(os.getenv('EMAIL_PORT', '1025'))
EMAIL_HOST_USER = os.getenv('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = os.getenv('EMAIL_HOST_PASSWORD', '')
EMAIL_USE_TLS = os.getenv('EMAIL_USE_TLS', '0') == '1'
EMAIL_USE_SSL = os.getenv('EMAIL_USE_SSL', '0') == '1'
EMAIL_TIMEOUT = 10
DEFAULT_FROM_EMAIL = os.getenv('DEFAULT_FROM_EMAIL', 'TenderSearch <no-reply@tender-search.kz>')
CSRF_TRUSTED_ORIGINS = [value for value in os.getenv("CSRF_TRUSTED_ORIGINS", "").split(",") if value]
INSTALLED_APPS = [
    "django.contrib.admin", "django.contrib.auth", "django.contrib.contenttypes",
    "django.contrib.sessions", "django.contrib.messages", "django.contrib.staticfiles",
    "tender_search.core",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "tender_search.core.auth_security.AuthResponseMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "tender_search.core.access.AccessMiddleware",
    "tender_search.core.seo.SearchIndexingMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "tender_search.web.urls"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [Path(__file__).resolve().parent.parent / "core" / "templates"], "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
        "tender_search.core.context_processors.favorite_counts",
        "tender_search.core.context_processors.account_access",
        "tender_search.core.seo.metadata",
        "tender_search.core.context_processors.procurement_coverage",
    ]},
}]
WSGI_APPLICATION = "tender_search.web.wsgi.application"
if os.getenv("TENDER_SQLITE") == "1":
    # Fast local tests only. Docker always uses PostgreSQL.
    DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "data/test.sqlite3"}}
else:
    DATABASES = {"default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.getenv("POSTGRES_DB", "tender_search"),
        "USER": os.getenv("POSTGRES_USER", "tender_search"),
        "PASSWORD": os.environ["POSTGRES_PASSWORD"],
        "HOST": os.getenv("POSTGRES_HOST", "db"), "PORT": "5432",
        "CONN_MAX_AGE": 60,
        # Search iterators consume the complete candidate stream. PostgreSQL's
        # default cursor plan assumes only 10% will be read and can choose a
        # slow ordered scan instead of the selective bitmap/trigram plan.
        "OPTIONS": {"options": "-c cursor_tuple_fraction=1.0"},
    }}
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
LANGUAGE_CODE = "ru"
TIME_ZONE = "Asia/Qyzylorda"
USE_I18N = True
USE_TZ = True
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
MEDIA_ROOT = BASE_DIR / "data/documents"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = os.getenv("HTTPS_ENABLED", "0") == "1"
CSRF_COOKIE_SECURE = SESSION_COOKIE_SECURE
SECURE_SSL_REDIRECT = SESSION_COOKIE_SECURE
# HTTPS service only; no preload or includeSubDomains on temporary tunnels.
SECURE_HSTS_SECONDS = 3600 if SESSION_COOKIE_SECURE else 0
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = TIME_ZONE
CELERY_TASK_SOFT_TIME_LIMIT = 240
CELERY_TASK_TIME_LIMIT = 300
CELERY_RESULT_EXPIRES = 3600
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_ROUTES = {
    'tender_search.core.tasks.web_search': {'queue': 'interactive'},
    'tender_search.core.tasks.extract_lot_specification': {'queue': 'specifications'},
}
USE_LOCAL_INDEX = True
LEAD_COLLECTION_AVAILABLE = os.getenv("LEAD_COLLECTION_AVAILABLE", "0") == "1"
SEARCH_DB_PREFILTERS = True
SEARCH_CANDIDATE_CACHE = False
SEARCH_CACHE_TTL = 120
SEARCH_CACHE_MAX_CANDIDATES = 10000
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
LOGGING = {"version": 1, "disable_existing_loggers": False,
           "filters": {"auth_links": {"()": "tender_search.core.auth_logging.RedactAuthLinks"}},
           "handlers": {"console": {"class": "logging.StreamHandler", "filters": ["auth_links"]}},
           "root": {"handlers": ["console"], "level": "INFO"}}

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_BOT_USERNAME = os.getenv("TELEGRAM_BOT_USERNAME", "").lstrip("@")
TELEGRAM_POLLING_ENABLED = os.getenv("TELEGRAM_POLLING_ENABLED", "true").lower() in ("1", "true", "yes")
