"""
Base settings shared by every environment.

Environment-specific overrides live in dev.py and prod.py.
Secrets are ALWAYS read from the environment via django-environ; never
hardcode a credential in this file.
"""

from pathlib import Path

import environ

# erp/config/settings/base.py -> erp/
BASE_DIR = Path(__file__).resolve().parents[2]

env = environ.Env(
    DEBUG=(bool, False),
    ALLOWED_HOSTS=(list, []),
    CSRF_TRUSTED_ORIGINS=(list, []),
    TIME_ZONE=(str, "Asia/Karachi"),
    LANGUAGE_CODE=(str, "en-us"),
)

# Read erp/.env when present. On Windows production this file sits next to
# manage.py and is the only place secrets are configured.
environ.Env.read_env(BASE_DIR / ".env")

# Placeholder so a fresh clone boots before .env exists. prod.py re-reads this
# with no default, so a missing SECRET_KEY hard-fails at startup on Windows.
SECRET_KEY = env("SECRET_KEY", default="insecure-development-key-do-not-deploy")
DEBUG = env("DEBUG")
ALLOWED_HOSTS = env("ALLOWED_HOSTS")
CSRF_TRUSTED_ORIGINS = env("CSRF_TRUSTED_ORIGINS")


# --------------------------------------------------------------------------
# Applications
# --------------------------------------------------------------------------
# django-unfold must precede django.contrib.admin so its templates win.
INSTALLED_APPS = [
    "unfold",
    "unfold.contrib.filters",
    "unfold.contrib.forms",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Local apps. Ledger-bearing apps depend on core + accounting, so they are
    # listed after them.
    "apps.core",
    "apps.accounting",
    "apps.masters",
    "apps.purchasing",
    "apps.sales",
    "apps.payments",
    "apps.reports",
    "apps.backup",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # WhiteNoise serves static/dist directly from the WSGI process. There is no
    # nginx in production; this is how CSS/JS/fonts reach the browser.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------
# SQLite in WAL mode. transaction_mode=IMMEDIATE takes the write lock at BEGIN
# instead of at first write, which removes the SQLITE_BUSY deadlock window
# between concurrent posting transactions.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "data" / "erp.sqlite3",
        "OPTIONS": {
            "transaction_mode": "IMMEDIATE",
            "timeout": 20,
            "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;",
        },
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LOGIN_URL = "admin:login"
LOGIN_REDIRECT_URL = "/"


# --------------------------------------------------------------------------
# I18N
# --------------------------------------------------------------------------
LANGUAGE_CODE = env("LANGUAGE_CODE")
TIME_ZONE = env("TIME_ZONE")
USE_I18N = True
USE_TZ = True


# --------------------------------------------------------------------------
# Static files
# --------------------------------------------------------------------------
# static/src  = authored + vendored sources (Tailwind input, vendored JS/fonts)
# static/dist = compiled output, COMMITTED to git so production never builds
STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static" / "dist"]
STATIC_ROOT = BASE_DIR / "staticfiles"

MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"


# --------------------------------------------------------------------------
# Unfold admin
# --------------------------------------------------------------------------
UNFOLD = {
    "SITE_TITLE": "Distribution ERP",
    "SITE_HEADER": "Distribution ERP",
    "SHOW_HISTORY": True,
    "SHOW_VIEW_ON_SITE": False,
}


# --------------------------------------------------------------------------
# Domain-wide constants
# --------------------------------------------------------------------------
# Money is stored as integer paisa everywhere. This is the only conversion
# factor in the system; see CLAUDE.md.
PAISA_PER_RUPEE = 100
CURRENCY_SYMBOL = "Rs"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {"format": "{asctime} {levelname} {name} {message}", "style": "{"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "verbose"},
    },
    "root": {"handlers": ["console"], "level": "INFO"},
}
