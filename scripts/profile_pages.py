"""Time the screens that aggregate the whole ledger, at realistic volume.

    python manage.py migrate     --settings=config.settings.profile
    python manage.py seed_volume --invoices 50000 --settings=config.settings.profile
    python scripts/profile_pages.py

Reports the best of three runs per page, the query count, and **how many rows
each page actually rendered** — a report that answers in 7ms because it matched
nothing has not been profiled, it has been skipped, and that is exactly the
mistake this script exists to have already made once.

Development only. It talks to `data/profile.sqlite3` and never to the
development or production database. See CLAUDE.md §14 for what the numbers mean.
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.profile")

import django

django.setup()

from django.conf import settings  # noqa: E402

# So `connection.queries` is populated. Never set on a real installation.
settings.DEBUG = True

from django.contrib.auth.models import User  # noqa: E402
from django.db import connection, reset_queries  # noqa: E402
from django.db.models import Count  # noqa: E402
from django.test import Client as TestClient  # noqa: E402

from apps.accounting.models import LedgerEntry  # noqa: E402
from apps.accounts.models import UserProfile  # noqa: E402
from apps.sales.models import SalesInvoice  # noqa: E402

#: Anything slower than this on a page somebody opens all day is a bug.
BUDGET_MS = 500


def rows_rendered(html: str) -> int:
    """Rows in the first table body. Crude, and enough to catch an empty page."""
    return html.split("<tbody", 1)[-1].count("<tr")


def busiest_client() -> int | None:
    """Profile the client ledger against the worst statement, not an empty one."""
    row = (
        LedgerEntry.objects.filter(party_type="CLIENT")
        .values("party_id")
        .annotate(n=Count("id"))
        .order_by("-n")
        .first()
    )
    return row["party_id"] if row else None


def main() -> int:
    print(
        f"volume: {SalesInvoice.objects.count():,} invoices, "
        f"{LedgerEntry.objects.count():,} ledger entries\n"
    )

    user, _ = User.objects.get_or_create(username="profiler")
    user.is_superuser = user.is_staff = True
    user.save()
    profile = UserProfile.for_user(user)
    profile.must_change_password = False
    profile.save()

    client = TestClient()
    client.force_login(user)

    pages = [
        ("dashboard", "/"),
        ("receivable ageing", "/reports/receivable-ageing/"),
        ("client ledger", f"/reports/client-ledger/?client={busiest_client()}"),
        ("trial balance", "/reports/trial-balance/"),
        ("day book", "/reports/day-book/"),
        ("sales list", "/sales/invoices/"),
        ("recovery", "/payments/recovery/"),
        ("stock balance", "/reports/stock-balance/"),
    ]

    print(f"{'page':<22} {'status':>6} {'ms (best of 3)':>15} {'queries':>8} {'rows':>7}")
    print("-" * 66)

    over_budget = []
    for name, url in pages:
        best = float("inf")
        status = queries = 0
        body = ""
        for _ in range(3):
            reset_queries()
            started = time.perf_counter()
            response = client.get(url)
            best = min(best, (time.perf_counter() - started) * 1000)
            status = response.status_code
            queries = len(connection.queries)
            body = response.content.decode(errors="ignore")

        rows = rows_rendered(body)
        note = ""
        if best > BUDGET_MS:
            note = f"  <-- over {BUDGET_MS}ms"
            over_budget.append((name, url, best, queries))
        elif rows == 0:
            note = "  <-- EMPTY, so this timing means nothing"
        print(f"{name:<22} {status:>6} {best:>15.0f} {queries:>8} {rows:>7}{note}")

    print()
    if over_budget:
        print("OVER BUDGET:")
        for name, url, ms, queries in over_budget:
            print(f"  {name}: {ms:.0f}ms, {queries} queries  ({url})")
        return 1

    print(f"every page under {BUDGET_MS}ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
