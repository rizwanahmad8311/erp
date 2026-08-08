"""
Base settings shared by every environment.

Environment-specific overrides live in dev.py and prod.py.
Secrets are ALWAYS read from the environment via django-environ; never
hardcode a credential in this file.
"""

from pathlib import Path

import environ
from django.urls import reverse_lazy

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
# The sidebar is grouped by what a person is doing rather than by Django app
# label, because "masters", "transactions", "accounting", "reports" and "setup"
# is how the office already talks about the work. The app a model happens to
# live in is an implementation detail nobody in the office should have to learn.
#
# Because the navigation is spelled out, "show_all_applications" is off: a model
# that is registered but not listed here is invisible, which is deliberate. Add
# the link when you add the model.
#
# reverse_lazy, not reverse: the URL conf is not loaded while settings are being
# read.


def _staff(request) -> bool:
    """Sidebar entries are for people who can open the admin at all."""
    return request.user.is_active and request.user.is_staff


UNFOLD = {
    "SITE_TITLE": "Distribution ERP",
    "SITE_HEADER": "Distribution ERP",
    "SHOW_HISTORY": True,
    "SHOW_VIEW_ON_SITE": False,
    "SIDEBAR": {
        "show_search": True,
        "show_all_applications": False,
        "navigation": [
            {
                "title": "Masters",
                "separator": True,
                "items": [
                    {
                        "title": "Items",
                        "icon": "inventory_2",
                        "link": reverse_lazy("admin:masters_item_changelist"),
                        "permission": _staff,
                    },
                    {
                        "title": "Item categories",
                        "icon": "category",
                        "link": reverse_lazy("admin:masters_itemcategory_changelist"),
                        "permission": _staff,
                    },
                    {
                        "title": "Clients",
                        "icon": "storefront",
                        "link": reverse_lazy("admin:masters_client_changelist"),
                        "permission": _staff,
                    },
                    {
                        "title": "Vendors",
                        "icon": "local_shipping",
                        "link": reverse_lazy("admin:masters_vendor_changelist"),
                        "permission": _staff,
                    },
                    {
                        "title": "Routes",
                        "icon": "route",
                        "link": reverse_lazy("admin:masters_route_changelist"),
                        "permission": _staff,
                    },
                    {
                        "title": "Sellers",
                        "icon": "badge",
                        "link": reverse_lazy("admin:masters_seller_changelist"),
                        "permission": _staff,
                    },
                    {
                        "title": "Route sellers",
                        "icon": "hub",
                        "link": reverse_lazy("admin:masters_routeseller_changelist"),
                        "permission": _staff,
                    },
                ],
            },
            {
                # Sales orders, invoices, deliveries, receipts and payments join
                # these as each app's documents arrive. The links go to the
                # keyboard entry screens, not to the admin changelists — the
                # admin is for looking things up, not for typing bills into.
                "title": "Transactions",
                "separator": True,
                "items": [
                    {
                        "title": "Purchase invoices",
                        "icon": "receipt",
                        "link": reverse_lazy("purchasing:list", kwargs={"slug": "invoices"}),
                        "permission": _staff,
                    },
                    {
                        "title": "Purchase returns",
                        "icon": "assignment_return",
                        "link": reverse_lazy("purchasing:list", kwargs={"slug": "returns"}),
                        "permission": _staff,
                    },
                ],
            },
            {
                "title": "Accounting",
                "separator": True,
                "items": [
                    {
                        "title": "Chart of accounts",
                        "icon": "account_tree",
                        "link": reverse_lazy("admin:accounting_account_changelist"),
                        "permission": _staff,
                    },
                    {
                        "title": "Ledger entries",
                        "icon": "receipt_long",
                        "link": reverse_lazy("admin:accounting_ledgerentry_changelist"),
                        "permission": _staff,
                    },
                    {
                        "title": "Stock entries",
                        "icon": "inventory",
                        "link": reverse_lazy("admin:accounting_stockentry_changelist"),
                        "permission": _staff,
                    },
                    {
                        "title": "Warehouses",
                        "icon": "warehouse",
                        "link": reverse_lazy("admin:accounting_warehouse_changelist"),
                        "permission": _staff,
                    },
                ],
            },
            {
                # Ageing, recovery, stock position and the day book land here.
                # apps.reports aggregates the ledger; it holds no models of its own.
                "title": "Reports",
                "separator": True,
                "items": [],
            },
            {
                "title": "Setup",
                "separator": True,
                "items": [
                    {
                        "title": "Document sequences",
                        "icon": "tag",
                        "link": reverse_lazy("admin:core_documentsequence_changelist"),
                        "permission": _staff,
                    },
                    {
                        "title": "Users",
                        "icon": "person",
                        "link": reverse_lazy("admin:auth_user_changelist"),
                        "permission": lambda request: request.user.is_superuser,
                    },
                    {
                        "title": "Groups",
                        "icon": "group",
                        "link": reverse_lazy("admin:auth_group_changelist"),
                        "permission": lambda request: request.user.is_superuser,
                    },
                ],
            },
        ],
    },
}


# --------------------------------------------------------------------------
# Domain-wide constants
# --------------------------------------------------------------------------
# Money is stored as integer paisa everywhere. This is the only conversion
# factor in the system; see CLAUDE.md.
PAISA_PER_RUPEE = 100
CURRENCY_SYMBOL = "Rs"

# Whether an issue may take an (item, warehouse) balance below zero.
#
# Off. A negative balance has no cost behind it, so the moving weighted average
# that every later issue is valued at has nothing to average — the deficit gets
# valued at whatever the last known rate was and quietly spreads into cost of
# goods sold. Turn it on only for an installation that genuinely invoices before
# the goods receipt is entered, and expect the valuation to be approximate while
# any balance is under water.
ALLOW_NEGATIVE_STOCK = env.bool("ALLOW_NEGATIVE_STOCK", default=False)

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
