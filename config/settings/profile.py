"""Settings for volume profiling. Never used by the app or the tests.

A database of its own, so `manage.py seed_volume` cannot be pointed at the
development database — or, one bad day, at a real one. `data/profile.sqlite3`
is git-ignored along with everything else in `data/`.

Everything else is dev: WAL, IMMEDIATE transactions and the same indexes, so a
timing taken here is a timing that means something.
"""

from .dev import *
from .dev import BASE_DIR, DATABASES

DATABASES["default"]["NAME"] = BASE_DIR / "data" / "profile.sqlite3"

# The dashboard caches for a minute, which would make the second measurement of
# every page a measurement of the cache. Profiling wants the cold path.
DASHBOARD_CACHE_SECONDS = 0
