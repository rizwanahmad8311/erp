"""Performance guards for the three screens that aggregate the whole ledger.

Timings in a test suite are usually a bad idea — they fail on a busy CI box and
teach people to ignore them. These are here anyway, with a deliberately loose
budget, because the failure they guard against is not "it got 10% slower". It is
somebody adding a query inside a per-row loop, which does not slow a page down
by 10%, it slows it down by 40x, and no amount of CI noise hides that.

**What actually costs.** Measured against 50,000 invoices and 285,000 ledger
entries: the cost of the recovery workspace, the receivable ageing ladder and
the dashboard scales with the number of **open items**, not with the size of the
ledger. Settled bills are cheap; unsettled ones are examined one at a time.

    open items    dashboard    ageing    recovery
         7,500        155ms     142ms       267ms
        15,000        286ms     238ms       451ms
        50,000      1,566ms   1,097ms     2,260ms

A distribution business writing 50,000 invoices a year and collecting most of
them carries a few thousand open items, which is the first row. The last row is
a business that has not been paid all year.

So the query-count guards below are the real assertions, and the timing is the
backstop. A page whose query count is constant cannot develop an N+1.
"""

from __future__ import annotations

import datetime as dt
import time

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.accounting.enums import PartyType
from apps.accounting.models import Account, LedgerEntry
from apps.masters.models import Client, Route, Seller

pytestmark = pytest.mark.django_db

#: Enough to make an N+1 obvious and few enough to build in a second or two.
CLIENTS = 60
INVOICES_PER_CLIENT = 20


@pytest.fixture
def a_busy_ledger(db, accounts, warehouses):
    """1,200 open items across 60 shops, written straight to the ledger.

    Not through the posting services: this fixture is about the *shape* of the
    read path, and posting 1,200 invoices properly would take a minute per run
    and measure the write path instead.
    """
    route = Route.objects.create(code="R-01", name="Saddar")
    seller = Seller.objects.create(code="S-01", name="Adnan")
    clients = Client.objects.bulk_create(
        [
            Client(
                code=f"C-{n:05d}",
                name=f"Shop {n}",
                route=route,
                seller=seller,
                credit_limit_paisa=100_000_000,
                credit_days=15,
            )
            for n in range(CLIENTS)
        ]
    )
    clients = list(Client.objects.all())

    receivable = Account.objects.get(code="1130")
    sales = Account.objects.get(code="4100")
    start = dt.date(2026, 1, 1)

    rows = []
    n = 0
    for client in clients:
        for i in range(INVOICES_PER_CLIENT):
            n += 1
            day = start + dt.timedelta(days=i * 7)
            common = {
                "posting_date": day,
                "voucher_type": "SalesInvoice",
                "voucher_id": n,
                "voucher_code": f"SI-2026-{n:06d}",
            }
            rows += [
                LedgerEntry(
                    account=receivable,
                    debit_paisa=100_000,
                    credit_paisa=0,
                    party_type=PartyType.CLIENT,
                    party_id=client.pk,
                    **common,
                ),
                LedgerEntry(account=sales, debit_paisa=0, credit_paisa=100_000, **common),
            ]
    LedgerEntry._base_manager.bulk_create(rows, batch_size=1000)
    return clients


def _admin(django_user_model, client):
    user = django_user_model.objects.create_superuser(
        username="perf", password="x", email="p@example.test"
    )
    from apps.accounts.models import UserProfile

    profile = UserProfile.for_user(user)
    profile.must_change_password = False
    profile.save()
    client.force_login(user)
    return user


class TestTheQueryCountIsConstant:
    """The assertion that actually catches regressions.

    A page that answers in a fixed number of queries whatever the row count
    cannot have grown an N+1. The exact numbers are upper bounds with headroom,
    not targets — raise them deliberately, never to make a red test go green.
    """

    @pytest.mark.parametrize(
        ("name", "url", "budget"),
        [
            ("dashboard", "/", 30),
            ("receivable ageing", "/reports/receivable-ageing/", 20),
            ("recovery", "/payments/recovery/", 25),
            ("trial balance", "/reports/trial-balance/", 15),
        ],
    )
    def test_it_does_not_grow_a_query_per_row(
        self, a_busy_ledger, client, django_user_model, name, url, budget
    ):
        _admin(django_user_model, client)

        with CaptureQueriesContext(connection) as captured:
            response = client.get(url)

        assert response.status_code == 200
        assert len(captured) <= budget, (
            f"{name} ran {len(captured)} queries over {CLIENTS} shops and "
            f"{CLIENTS * INVOICES_PER_CLIENT} open items — budget is {budget}. "
            f"That is the shape of a query inside a loop."
        )


class TestTheBudget:
    """The backstop. Loose on purpose — see the module docstring."""

    #: Generous: the same work measures ~270ms on a laptop at six times this
    #: volume. A failure here is a change in complexity, not in constant factor.
    BUDGET_MS = 2_000

    @pytest.mark.parametrize(
        ("name", "url"),
        [
            ("dashboard", "/"),
            ("receivable ageing", "/reports/receivable-ageing/"),
            ("recovery", "/payments/recovery/"),
        ],
    )
    def test_it_answers_within_the_budget(
        self, a_busy_ledger, client, django_user_model, name, url
    ):
        _admin(django_user_model, client)

        client.get(url)  # warm any lazily-built caches
        started = time.perf_counter()
        response = client.get(url)
        elapsed_ms = (time.perf_counter() - started) * 1000

        assert response.status_code == 200
        assert elapsed_ms < self.BUDGET_MS, (
            f"{name} took {elapsed_ms:.0f}ms over {CLIENTS * INVOICES_PER_CLIENT} "
            f"open items (budget {self.BUDGET_MS}ms)."
        )


class TestTheCoveringIndexIsStillThere:
    def test_the_recovery_group_by_is_answered_from_the_index(self, a_busy_ledger):
        """`ledger_party_voucher_idx` exists so this GROUP BY needs no temp B-tree.

        Measured at 200,000 entries: 178ms with a temp B-tree, 94ms covering.
        Asserted through the query plan rather than through a timing, because
        the plan is the thing that would silently change.
        """
        if connection.vendor != "sqlite":  # pragma: no cover - SQLite in prod
            pytest.skip("query-plan text is SQLite-specific")

        with connection.cursor() as cursor:
            cursor.execute(
                "EXPLAIN QUERY PLAN "
                "SELECT party_id, voucher_type, voucher_id, voucher_code, "
                "  SUM(debit_paisa), SUM(credit_paisa) "
                "FROM accounting_ledgerentry "
                "WHERE party_type = 'CLIENT' AND posting_date <= '2026-12-31' "
                "GROUP BY party_id, voucher_type, voucher_id, voucher_code"
            )
            plan = " ".join(row[-1] for row in cursor.fetchall())

        assert "ledger_party_voucher_idx" in plan, f"plan was: {plan}"
        assert "TEMP B-TREE" not in plan.upper(), (
            f"the GROUP BY fell back to a sort — the covering index is not being used. Plan: {plan}"
        )
