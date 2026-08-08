"""
Production settings — a single Windows PC on an office LAN.

Hard constraints that shape this file:
  * no Docker, no nginx, no node, no compiler, no internet
  * served by waitress (see serve.py), static files by WhiteNoise
  * plain HTTP over the LAN by default; there is no certificate authority
"""

from .base import *
from .base import BASE_DIR, env

DEBUG = False

# No defaults here on purpose: both raise ImproperlyConfigured at startup if
# .env is missing them, which is the behaviour we want on an unattended PC.
SECRET_KEY = env("SECRET_KEY")
# e.g. ALLOWED_HOSTS=192.168.1.50,erp-pc,localhost
ALLOWED_HOSTS = env("ALLOWED_HOSTS")

# --------------------------------------------------------------------------
# Static files
# --------------------------------------------------------------------------
# Compress + hash at collectstatic time. Nothing is fetched from a network:
# every asset already exists in static/dist, committed to git.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

# A template referencing an asset that is not in dist should render a plain URL
# rather than crash the whole page on an office PC with nobody to debug it.
WHITENOISE_MANIFEST_STRICT = False
WHITENOISE_MAX_AGE = 31536000

# --------------------------------------------------------------------------
# Security
# --------------------------------------------------------------------------
# The LAN is HTTP-only, so TLS-dependent hardening is opt-in via .env rather
# than on by default — enabling it without a certificate locks users out.
USE_TLS = env.bool("USE_TLS", default=False)

SECURE_SSL_REDIRECT = USE_TLS
SESSION_COOKIE_SECURE = USE_TLS
CSRF_COOKIE_SECURE = USE_TLS
SECURE_HSTS_SECONDS = 31536000 if USE_TLS else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = USE_TLS

SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
SESSION_COOKIE_HTTPONLY = True
SESSION_EXPIRE_AT_BROWSER_CLOSE = False

CSRF_TRUSTED_ORIGINS = env("CSRF_TRUSTED_ORIGINS")

# --------------------------------------------------------------------------
# Logging to disk — there is no log aggregator on this machine.
# --------------------------------------------------------------------------
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {"format": "{asctime} {levelname} {name} {message}", "style": "{"},
    },
    "handlers": {
        "file": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": LOG_DIR / "erp.log",
            "maxBytes": 5 * 1024 * 1024,
            "backupCount": 5,
            "formatter": "verbose",
            "encoding": "utf-8",
        },
        "console": {"class": "logging.StreamHandler", "formatter": "verbose"},
    },
    "root": {"handlers": ["file", "console"], "level": "INFO"},
    "loggers": {
        "django.request": {
            "handlers": ["file", "console"],
            "level": "ERROR",
            "propagate": False,
        },
    },
}
