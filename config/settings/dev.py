"""Development settings — macOS host, running inside Docker."""

from .base import *
from .base import BASE_DIR, INSTALLED_APPS, env  # noqa: F401

DEBUG = True

# Docker publishes 8000 on the host; the container itself is not internet
# facing, so a permissive host list is fine here and only here.
ALLOWED_HOSTS = ["*"]

EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# Serve static/dist unminified and unhashed; no manifest to rebuild on edit.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

INTERNAL_IPS = ["127.0.0.1"]
