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
    # Row history for MASTER data only — see apps/masters/models.py. Documents
    # are deliberately not registered: the ledger is already their audit log and
    # a POSTED one cannot change (CLAUDE.md §3, §5).
    "simple_history",
    # Local apps. Ledger-bearing apps depend on core + accounting, so they are
    # listed after them.
    "apps.core",
    # Who may do what. Listed after core (it uses TimeStampedModel) and before
    # everything that checks a permission, which is all of them.
    "apps.accounts",
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
    # Puts the logged-in user onto each historical master row. It must come
    # after AuthenticationMiddleware, which is what puts the user on the
    # request in the first place. Without it a master edited through a screen
    # records what changed and not who changed it.
    "simple_history.middleware.HistoryRequestMiddleware",
    # A login created by an administrator has a password that administrator
    # knows, so "who posted this invoice" has two possible answers until it is
    # changed. This redirects every page to the password screen until it is.
    # After AuthenticationMiddleware, for the same reason as the line above.
    "apps.accounts.middleware.ForcePasswordChangeMiddleware",
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
                # The company letterhead, for the @media print path. Lazy, so a
                # page that never prints a header never queries for one.
                "apps.reports.context_processors.company",
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


def _may(*permissions, any_of=False):
    """A sidebar ``permission`` callable behind the real permission.

    Imported lazily inside the function body because settings are read before
    the app registry is ready, and ``apps.accounts.access`` imports Django auth.
    """

    def check(request) -> bool:
        from apps.accounts.access import can_staff

        return can_staff(*permissions, any_of=any_of)(request)

    return check


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
                        "permission": _may("masters.view_item"),
                    },
                    {
                        "title": "Item categories",
                        "icon": "category",
                        "link": reverse_lazy("admin:masters_itemcategory_changelist"),
                        "permission": _may("masters.view_itemcategory"),
                    },
                    {
                        "title": "Clients",
                        "icon": "storefront",
                        "link": reverse_lazy("admin:masters_client_changelist"),
                        "permission": _may("masters.view_client"),
                    },
                    {
                        "title": "Vendors",
                        "icon": "local_shipping",
                        "link": reverse_lazy("admin:masters_vendor_changelist"),
                        "permission": _may("masters.view_vendor"),
                    },
                    {
                        "title": "Routes",
                        "icon": "route",
                        "link": reverse_lazy("admin:masters_route_changelist"),
                        "permission": _may("masters.view_route"),
                    },
                    {
                        "title": "Sellers",
                        "icon": "badge",
                        "link": reverse_lazy("admin:masters_seller_changelist"),
                        "permission": _may("masters.view_seller"),
                    },
                    {
                        "title": "Route sellers",
                        "icon": "hub",
                        "link": reverse_lazy("admin:masters_routeseller_changelist"),
                        "permission": _may("masters.view_routeseller"),
                    },
                ],
            },
            {
                # Sales comes first: it is what the office does all day. The links go to the
                # keyboard entry screens, not to the admin changelists — the
                # admin is for looking things up, not for typing bills into.
                "title": "Transactions",
                "separator": True,
                "items": [
                    {
                        "title": "Sales invoices",
                        "icon": "point_of_sale",
                        "link": reverse_lazy("sales:list", kwargs={"slug": "invoices"}),
                        "permission": _may("sales.view_salesinvoice"),
                    },
                    {
                        "title": "Credit notes",
                        "icon": "assignment_returned",
                        "link": reverse_lazy("sales:list", kwargs={"slug": "returns"}),
                        "permission": _may("sales.view_salesreturn"),
                    },
                    {
                        "title": "Purchase invoices",
                        "icon": "receipt",
                        "link": reverse_lazy("purchasing:list", kwargs={"slug": "invoices"}),
                        "permission": _may("purchasing.view_purchaseinvoice"),
                    },
                    {
                        "title": "Purchase returns",
                        "icon": "assignment_return",
                        "link": reverse_lazy("purchasing:list", kwargs={"slug": "returns"}),
                        "permission": _may("purchasing.view_purchasereturn"),
                    },
                    {
                        "title": "Receipts & payments",
                        "icon": "payments",
                        "link": reverse_lazy("payments:list"),
                        "permission": _may("payments.view_payment"),
                    },
                ],
            },
            {
                # The screen the accountant lives in, and the drawer beside it.
                # Both aggregate the ledger; neither holds a figure of its own.
                "title": "Recovery",
                "separator": True,
                "items": [
                    {
                        "title": "Recovery workspace",
                        "icon": "request_quote",
                        "link": reverse_lazy("payments:recovery"),
                        "permission": _may("payments.view_payment"),
                    },
                    {
                        "title": "Cheques in hand",
                        "icon": "account_balance",
                        "link": reverse_lazy("payments:cheques"),
                        "permission": _may("payments.view_chequeevent"),
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
                        "permission": _may("accounting.view_account"),
                    },
                    {
                        "title": "Ledger entries",
                        "icon": "receipt_long",
                        "link": reverse_lazy("admin:accounting_ledgerentry_changelist"),
                        "permission": _may("accounting.view_ledgerentry"),
                    },
                    {
                        "title": "Stock entries",
                        "icon": "inventory",
                        "link": reverse_lazy("admin:accounting_stockentry_changelist"),
                        "permission": _may("accounting.view_stockentry"),
                    },
                    {
                        "title": "Warehouses",
                        "icon": "warehouse",
                        "link": reverse_lazy("admin:accounting_warehouse_changelist"),
                        "permission": _may("accounting.view_warehouse"),
                    },
                ],
            },
            {
                # apps.reports aggregates the ledger and owns exactly one model
                # — the company profile printed at the top of every page.
                #
                # Only the four that get opened daily are listed. The rest are
                # one click further in, on the index, which is built from the
                # registry rather than from a list somebody keeps in step by
                # hand — see apps/reports/registry.py. A sidebar with eighteen
                # entries in it is a sidebar nobody reads.
                "title": "Reports",
                "separator": True,
                "items": [
                    {
                        "title": "All reports",
                        "icon": "lab_profile",
                        "link": reverse_lazy("reports:index"),
                        "permission": _may("reports.view_reports"),
                    },
                    {
                        "title": "Trial balance",
                        "icon": "balance",
                        "link": reverse_lazy("reports:report", kwargs={"slug": "trial-balance"}),
                        "permission": _may("reports.view_reports_financial"),
                    },
                    {
                        "title": "Day book",
                        "icon": "today",
                        "link": reverse_lazy("reports:report", kwargs={"slug": "day-book"}),
                        "permission": _may("reports.view_reports"),
                    },
                    {
                        "title": "Route day sheet",
                        "icon": "local_shipping",
                        "link": reverse_lazy("reports:report", kwargs={"slug": "route-day-sheet"}),
                        "permission": _may("reports.view_reports"),
                    },
                    {
                        "title": "Stock balance",
                        "icon": "inventory",
                        "link": reverse_lazy("reports:report", kwargs={"slug": "stock-balance"}),
                        "permission": _may("reports.view_reports"),
                    },
                ],
            },
            {
                "title": "Setup",
                "separator": True,
                "items": [
                    {
                        "title": "Company profile",
                        "icon": "domain",
                        "link": reverse_lazy("admin:reports_companyprofile_changelist"),
                        "permission": _may("reports.change_companyprofile"),
                    },
                    {
                        "title": "Document sequences",
                        "icon": "tag",
                        "link": reverse_lazy("admin:core_documentsequence_changelist"),
                        "permission": _may("core.view_documentsequence"),
                    },
                    {
                        "title": "Users",
                        "icon": "person",
                        "link": reverse_lazy("admin:auth_user_changelist"),
                        "permission": _may("accounts.manage_users"),
                    },
                    {
                        "title": "Groups",
                        "icon": "group",
                        "link": reverse_lazy("admin:auth_group_changelist"),
                        "permission": _may("auth.view_group"),
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

# --------------------------------------------------------------------------
# Printing
# --------------------------------------------------------------------------
# PDFs are drawn by ReportLab (apps/reports/pdf/) — a pure-Python wheel, which
# is the whole reason it was chosen over WeasyPrint, xhtml2pdf, wkhtmltopdf or
# headless Chrome. Every one of those needs a system library or a binary that
# turns the six-line Windows install in CLAUDE.md §8 into a support call.
#
# Which typeface a PDF is set in. A family here is looked for in
# static/src/fonts/ as <Family>-Regular.ttf and friends — the same directory the
# browser reads its WOFF2 from, so print and screen match (CLAUDE.md §7).
# Nothing is downloaded: when a family is not vendored, ReportLab's built-in
# Helvetica and Courier are used, which is what the current system-font stacks
# in static/src/css/app.css resolve to anyway.
PDF_FONT_FAMILY = env("PDF_FONT_FAMILY", default="Inter")
PDF_MONO_FONT_FAMILY = env("PDF_MONO_FONT_FAMILY", default="JetBrainsMono")

# Which layout a payment receipt prints on, per machine. The counter PC drives
# an 80mm thermal roll; the back office prints A5 for the file. Set it in .env
# on each machine — a ?layout= on the URL overrides it for one job.
# One of: a4, a5, 80mm, 58mm.
RECEIPT_LAYOUT = env("RECEIPT_LAYOUT", default="a5")

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
