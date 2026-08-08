"""
Test settings. Used by pytest (see pyproject.toml) and by nothing else.

Differs from dev in exactly two ways, both of which matter:

1. ``tests.testapp`` is installed, giving the abstract bases in apps.core a
   concrete table to be tested against.
2. The test database is a **file**, not the in-memory database Django defaults
   to for SQLite. The concurrency test in tests/test_sequences.py runs real
   threads against real connections; an in-memory shared-cache database does not
   reproduce the WAL locking and ``BEGIN IMMEDIATE`` behaviour that production
   relies on, so the test would prove nothing.
"""

from .dev import *
from .dev import BASE_DIR, DATABASES, INSTALLED_APPS

INSTALLED_APPS = [*INSTALLED_APPS, "tests.testapp"]

DATABASES["default"]["TEST"] = {"NAME": str(BASE_DIR / "data" / "test_erp.sqlite3")}

# Fast and deterministic; nothing here tests password hashing.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# Keep test output quiet unless something actually fails.
LOGGING["root"]["level"] = "WARNING"
