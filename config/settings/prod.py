"""
Production settings — a single Windows PC on an office LAN.

Hard constraints that shape this file:
  * no Docker, no nginx, no node, no compiler, no internet
  * served by waitress (see serve.py), static files by WhiteNoise
  * plain HTTP over the LAN by default; there is no certificate authority
"""

import ipaddress

from django.core.exceptions import ImproperlyConfigured

from .base import *
from .base import BASE_DIR, env

DEBUG = False

# No defaults here on purpose: both raise ImproperlyConfigured at startup if
# .env is missing them, which is the behaviour we want on an unattended PC.
SECRET_KEY = env("SECRET_KEY")
# e.g. ALLOWED_HOSTS=192.168.1.50,erp-pc,localhost
ALLOWED_HOSTS = env("ALLOWED_HOSTS")

# --------------------------------------------------------------------------
# The LAN subnet
# --------------------------------------------------------------------------
# Django's ALLOWED_HOSTS is a list of literal host strings — it understands no
# CIDR and no wildcard except a bare "*", which switches the check off
# altogether. That leaves an office LAN in an awkward spot: every counter PC
# reaches the server by typing its IP address, and the day DHCP hands the
# server a different address every one of them gets a 400 with nobody on site
# who knows why.
#
# So: name the subnet once and expand it. ALLOWED_HOSTS_SUBNET=192.168.1.0/24
# becomes the 254 usable addresses in it, appended to whatever ALLOWED_HOSTS
# already lists. The check stays a real check — a Host header from outside the
# subnet is still refused, which is what stops DNS rebinding — and the server's
# address may move within the LAN without anybody editing a file.
#
# `install.bat` detects the subnet from the machine's own adapter and writes it
# in, so on a normal install nobody types this at all.
ALLOWED_HOSTS_SUBNET = env("ALLOWED_HOSTS_SUBNET", default="")

#: A /20 is 4,094 hosts and already far past what an office has. The cap is
#: here so a typed /8 fails loudly at startup rather than building a list of
#: sixteen million strings that Django scans on every request.
MAX_SUBNET_HOSTS = 4096

if ALLOWED_HOSTS_SUBNET:
    try:
        _network = ipaddress.ip_network(ALLOWED_HOSTS_SUBNET, strict=False)
    except ValueError as exc:
        raise ImproperlyConfigured(
            f"ALLOWED_HOSTS_SUBNET={ALLOWED_HOSTS_SUBNET!r} in .env is not a subnet. "
            f"It should look like 192.168.1.0/24. ({exc})"
        ) from exc
    if _network.num_addresses > MAX_SUBNET_HOSTS:
        raise ImproperlyConfigured(
            f"ALLOWED_HOSTS_SUBNET={ALLOWED_HOSTS_SUBNET} covers {_network.num_addresses} "
            f"addresses, which is more than this is meant for. Use the office's own "
            f"subnet — usually a /24, such as 192.168.1.0/24."
        )
    ALLOWED_HOSTS = [*ALLOWED_HOSTS, *(str(host) for host in _network.hosts())]

if not ALLOWED_HOSTS:
    raise ImproperlyConfigured(
        "ALLOWED_HOSTS is empty, so this server would refuse every request. "
        "Set ALLOWED_HOSTS in .env — at minimum localhost,127.0.0.1 plus this "
        "PC's LAN address — or set ALLOWED_HOSTS_SUBNET to the office subnet."
    )

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
# C:\erp\logs on the office PC. Rotating and capped, because this runs as a
# service on a machine nobody logs into for months: an uncapped log is a disk
# that fills, and a full disk stops the ERP and the backups together.
#
# parents=True because the service account may start before anything has
# created C:\erp — a logging config that raises leaves waitress dead with the
# reason only in the Windows event log.
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

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
