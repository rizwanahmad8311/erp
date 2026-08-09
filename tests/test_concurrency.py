"""Ten operators posting at the same instant.

``tests/test_sequences.py`` already proves that two threads cannot be handed the
same document *number*. This proves the thing the office actually does: ten
people press Post at once, each writing a document header, its lines, its ledger
entries and its stock entries inside one transaction.

That is a much bigger claim than the sequence test, because a full posting holds
SQLite's write lock for the whole transaction and does far more work while
holding it. Two failure modes are being ruled out:

* **A duplicate document code**, which is silent and permanent — two invoices
  with the same number in the same year, discovered by an auditor.
* **``database is locked``**, which is loud, and which is what SQLite does when
  a second writer waits longer than ``timeout`` for the lock. Ten counter staff
  posting within the same second is not an unusual afternoon.

Both are prevented by the same two settings, and this is the test that says so:
``transaction_mode: IMMEDIATE`` takes the write lock at ``BEGIN`` rather than at
first write, and ``timeout: 20`` gives a waiting writer twenty seconds to get
it. ``select_for_update()`` in ``get_next_code`` is a no-op on SQLite and is
kept for correctness on a row-locking backend (CLAUDE.md §5).

These need ``transaction=True`` so each thread gets a real connection against
the real on-disk test database. An in-memory database does not reproduce WAL
locking, and a test that ran against one would prove nothing at all.
"""

from __future__ import annotations

import datetime as dt
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from django.db import connection
from django.db.models import Sum

from apps.accounting.models import Account, LedgerEntry, StockEntry, Warehouse
from apps.core.enums import DocumentStatus
from apps.masters.enums import Unit
from apps.masters.models import Client, Item, Vendor
from apps.purchasing import services as purchasing
from apps.purchasing.models import PurchaseInvoiceLine
from apps.sales import services as sales
from apps.sales.models import SalesInvoice, SalesInvoiceLine

pytestmark = pytest.mark.django_db(transaction=True)

#: The brief's number, and a fair description of a counter at 6pm.
OPERATORS = 10

APRIL = dt.date(2026, 4, 1)
MAY = dt.date(2026, 5, 1)


@pytest.fixture
def ready_to_trade(db):
    """Chart, warehouse, stock on hand, and ten shops to bill.

    Enough stock that none of the ten postings is refused for being short —
    a refusal is correct behaviour but it would take that thread out of the
    race, and the race is the thing being tested.
    """
    from apps.accounting.chart import seed_chart_of_accounts

    seed_chart_of_accounts(Account)
    warehouse = Warehouse.objects.filter(is_default=True).first() or Warehouse.objects.create(
        code="MAIN", name="Main Godown", is_default=True
    )
    vendor = Vendor.objects.create(code="V-01", name="Supplier")
    item = Item.objects.create(code="OIL-1", name="Cooking Oil 1L", carton_size=12)

    bill = purchasing.create_purchase_invoice(
        vendor=vendor, warehouse=warehouse, posting_date=APRIL
    )
    purchasing.update_line(
        PurchaseInvoiceLine(document=bill),
        item=item,
        qty_input=1_000,
        unit_input=Unit.CARTON,
        rate_input_paisa=240_000,
    ).save()
    purchasing.post_purchase_invoice(bill, user=None)

    shops = [
        Client.objects.create(
            code=f"C-{n:04d}", name=f"Shop {n}", credit_limit_paisa=100_000_000, credit_days=15
        )
        for n in range(OPERATORS)
    ]
    return warehouse, item, shops


def _post_one(barrier, warehouse_id, item_id, client_id):
    """One operator's whole job: draft, line, post. Runs in its own thread.

    The barrier is what makes this a race rather than a queue — every thread
    waits at it and they are all released into ``post_sales_invoice`` together.
    """
    try:
        barrier.wait(timeout=60)
        warehouse = Warehouse.objects.get(pk=warehouse_id)
        item = Item.objects.get(pk=item_id)
        client = Client.objects.get(pk=client_id)

        invoice = sales.create_sales_invoice(client=client, warehouse=warehouse, posting_date=MAY)
        sales.update_line(
            SalesInvoiceLine(document=invoice),
            item=item,
            qty_input=1,
            unit_input=Unit.CARTON,
            rate_input_paisa=250_000,
        ).save()
        posted = sales.post_sales_invoice(invoice, user=None)
        return ("ok", posted.code)
    except Exception as exc:  # returned, not raised: the caller reports all ten
        return ("error", f"{type(exc).__name__}: {exc}")
    finally:
        # Each thread opened its own connection. Leaking them wedges teardown.
        connection.close()


class TestTenOperatorsPostingAtOnce:
    def test_every_posting_succeeds(self, ready_to_trade):
        """No ``database is locked``, no deadlock, no lost posting."""
        warehouse, item, shops = ready_to_trade
        barrier = threading.Barrier(OPERATORS)

        with ThreadPoolExecutor(max_workers=OPERATORS) as pool:
            results = [
                f.result(timeout=120)
                for f in [
                    pool.submit(_post_one, barrier, warehouse.pk, item.pk, shop.pk)
                    for shop in shops
                ]
            ]

        errors = [detail for status, detail in results if status == "error"]
        assert not errors, "postings failed under contention:\n  " + "\n  ".join(errors)

        locked = [e for e in errors if "locked" in e.lower()]
        assert not locked, (
            "SQLite reported 'database is locked'. transaction_mode=IMMEDIATE and "
            "timeout=20 are what prevent this — check config/settings/base.py."
        )

    def test_no_two_invoices_share_a_code(self, ready_to_trade):
        """The silent failure: two SI-2026-000004s, found by an auditor in a year."""
        warehouse, item, shops = ready_to_trade
        barrier = threading.Barrier(OPERATORS)

        with ThreadPoolExecutor(max_workers=OPERATORS) as pool:
            results = [
                f.result(timeout=120)
                for f in [
                    pool.submit(_post_one, barrier, warehouse.pk, item.pk, shop.pk)
                    for shop in shops
                ]
            ]

        codes = [detail for status, detail in results if status == "ok"]
        assert len(codes) == OPERATORS
        assert len(set(codes)) == OPERATORS, f"duplicate codes: {sorted(codes)}"

        stored = list(SalesInvoice.objects.values_list("code", flat=True))
        assert len(set(stored)) == len(stored), "the database holds a duplicate code"

    def test_the_books_balance_afterwards(self, ready_to_trade):
        """Ten interleaved transactions, and the ledger still sums to zero.

        The strongest assertion here. Each posting balances inside its own
        transaction (CLAUDE.md §4); if the lock let two of them interleave
        halfway, this is what would notice.
        """
        warehouse, item, shops = ready_to_trade
        barrier = threading.Barrier(OPERATORS)

        with ThreadPoolExecutor(max_workers=OPERATORS) as pool:
            list(pool.map(lambda shop: _post_one(barrier, warehouse.pk, item.pk, shop.pk), shops))

        totals = LedgerEntry.objects.aggregate(d=Sum("debit_paisa"), c=Sum("credit_paisa"))
        assert (totals["d"] or 0) == (totals["c"] or 0), (
            f"trial balance out by {(totals['d'] or 0) - (totals['c'] or 0)} paisa "
            f"after {OPERATORS} concurrent postings"
        )

    def test_every_posting_wrote_a_complete_set_of_rows(self, ready_to_trade):
        """No half-written posting: a header with no entries, or entries with no stock.

        This is what ``transaction.atomic()`` promises, asserted under the one
        condition that would break it.
        """
        warehouse, item, shops = ready_to_trade
        barrier = threading.Barrier(OPERATORS)

        with ThreadPoolExecutor(max_workers=OPERATORS) as pool:
            list(pool.map(lambda shop: _post_one(barrier, warehouse.pk, item.pk, shop.pk), shops))

        posted = SalesInvoice.objects.filter(status=DocumentStatus.POSTED)
        assert posted.count() == OPERATORS

        for invoice in posted:
            entries = LedgerEntry.objects.filter(voucher_type="SalesInvoice", voucher_id=invoice.pk)
            movements = StockEntry.objects.filter(
                voucher_type="SalesInvoice", voucher_id=invoice.pk
            )
            assert entries.exists(), f"{invoice.code} posted but wrote no ledger entries"
            assert movements.exists(), f"{invoice.code} posted but moved no stock"

            totals = entries.aggregate(d=Sum("debit_paisa"), c=Sum("credit_paisa"))
            assert totals["d"] == totals["c"], f"{invoice.code} does not balance on its own"

    def test_the_sequence_counter_matches_what_was_issued(self, ready_to_trade):
        """No gap and no overlap: ten postings, counter at ten."""
        from apps.core.models import DocumentSequence

        warehouse, item, shops = ready_to_trade
        barrier = threading.Barrier(OPERATORS)

        with ThreadPoolExecutor(max_workers=OPERATORS) as pool:
            list(pool.map(lambda shop: _post_one(barrier, warehouse.pk, item.pk, shop.pk), shops))

        sequence = DocumentSequence.objects.get(prefix="SI", fiscal_year=2026)
        assert sequence.last_number == OPERATORS
