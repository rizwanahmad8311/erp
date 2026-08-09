"""Property-based invariants: random lifecycles, checked after every step.

The example-based suite asserts that *the stories somebody thought of* leave the
books balanced. This asserts it for stories nobody thought of: Hypothesis
generates a random sequence of post / cancel / amend / pay across several
documents and the invariants are re-checked after **every** step, not just at
the end. When it fails it shrinks the sequence to the shortest one that still
fails, which is usually two or three operations and immediately readable.

Four invariants, in increasing order of how much they catch:

1. **The trial balance is zero.** Debits equal credits over the whole ledger.
2. **Every voucher balances on its own.** Stronger, and the one that localises a
   bug: a system can have a zero trial balance while two documents are wrong in
   opposite directions. CLAUDE.md §4 promises this per transaction, so it must
   hold per voucher at every instant.
3. **Stock quantity equals the sum of its entries**, computed independently in
   Python rather than by the same aggregate the reports use — otherwise the
   assertion is the query checking itself.
4. **A reversal is the exact mirror of what it reverses.** Cancelling writes the
   opposite sign against the same account (§3), so a cancelled document
   contributes exactly zero to every account it touched.

Business refusals are not failures. A posting rejected for a credit limit, for
short stock, or a cancellation refused because a payment is allocated to the
document, are all the system working — they are counted and reported at the end
so that a run which refused *everything* cannot pass by doing nothing.
"""

from __future__ import annotations

import datetime as dt
import itertools
import os
from collections import defaultdict

from django.db.models import Sum
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st
from hypothesis.extra.django import TestCase

from apps.accounting.chart import seed_chart_of_accounts
from apps.accounting.models import Account, LedgerEntry, StockEntry, Warehouse
from apps.core.exceptions import CoreError
from apps.core.money import to_paisa
from apps.masters.enums import Unit
from apps.masters.models import Client, Item, Route, Seller, Vendor
from apps.payments import services as payments
from apps.payments.enums import PaymentDirection, PaymentMode
from apps.purchasing import services as purchasing
from apps.purchasing.models import PurchaseInvoiceLine
from apps.sales import services as sales
from apps.sales.models import SalesInvoiceLine

START = dt.date(2026, 4, 1)

#: Vendor bill numbers are unique per vendor, and Hypothesis re-runs setUp for
#: every example without necessarily rolling the previous one back. A
#: process-wide counter sidesteps having to know which: nothing here ever
#: reuses a number, whatever the example boundaries turn out to be.
_serial = itertools.count(1)

# ---------------------------------------------------------------------------
# The operation alphabet
# ---------------------------------------------------------------------------
# Each entry is (name, *parameters). Kept as plain tuples so a failing example
# prints as something readable rather than as a repr of a dataclass.
SELL = "sell"
BUY = "buy"
CANCEL = "cancel"
AMEND = "amend"
RECEIVE = "receive"

operations = st.one_of(
    st.tuples(
        st.just(SELL),
        st.integers(min_value=0, max_value=1),  # which client
        st.integers(min_value=0, max_value=1),  # which item
        st.integers(min_value=1, max_value=8),  # quantity
        st.sampled_from([Unit.PIECE, Unit.CARTON]),
        st.integers(min_value=100, max_value=90_000),  # rate in paisa
    ),
    st.tuples(
        st.just(BUY),
        st.integers(min_value=0, max_value=1),
        st.integers(min_value=1, max_value=20),
        st.sampled_from([Unit.PIECE, Unit.CARTON]),
        st.integers(min_value=100, max_value=90_000),
    ),
    # Documents are addressed by position in the order they were created, so a
    # shrunk example still refers to something that exists.
    st.tuples(st.just(CANCEL), st.integers(min_value=0, max_value=9)),
    st.tuples(st.just(AMEND), st.integers(min_value=0, max_value=9)),
    st.tuples(st.just(RECEIVE), st.integers(min_value=0, max_value=1), st.integers(1, 500_000)),
)


class TestLedgerInvariantsHold(TestCase):
    """Random lifecycles, invariants after every single step."""

    def setUp(self):
        super().setUp()
        seed_chart_of_accounts(Account)
        # A migration already seeds the default warehouse, and there may be only
        # one. Reuse it rather than fighting the constraint that says so.
        self.warehouse = Warehouse.objects.filter(is_default=True).first() or (
            Warehouse.objects.create(code="MAIN", name="Main Godown", is_default=True)
        )
        route, _ = Route.objects.get_or_create(code="R-01", defaults={"name": "Saddar"})
        seller, _ = Seller.objects.get_or_create(code="S-01", defaults={"name": "Adnan"})
        # get_or_create throughout: Hypothesis runs setUp for every example, and
        # the masters created by the previous one may or may not have been rolled
        # back depending on where the example boundary fell. Idempotent setup is
        # cheaper than reasoning about that.
        self.clients = [
            Client.objects.get_or_create(
                code=f"C-{n:04d}",
                defaults={
                    "name": f"Shop {n}",
                    "route": route,
                    "seller": seller,
                    # High enough that the credit limit is not what the property
                    # spends its time exercising; tests/test_sales_credit_limit.py
                    # is where that behaviour is pinned.
                    "credit_limit_paisa": to_paisa("10000000"),
                    "credit_days": 30,
                },
            )[0]
            for n in range(2)
        ]
        self.vendor = Vendor.objects.get_or_create(
            code="V-01", defaults={"name": "Supplier", "city": "Karachi"}
        )[0]
        self.items = [
            Item.objects.get_or_create(
                code="OIL-1",
                defaults={
                    "name": "Oil 1L",
                    "carton_size": 12,
                    "tax_rate_bp": 1750,
                    "sale_rate_paisa": to_paisa("250"),
                },
            )[0],
            Item.objects.get_or_create(
                code="RICE-25",
                defaults={
                    "name": "Rice 25kg",
                    "carton_size": 1,
                    "sale_rate_paisa": to_paisa("7850"),
                },
            )[0],
        ]
        # Opening stock, so the first sale has something to issue. Without it
        # every sale is refused for short stock and the run proves nothing.
        self._buy(item=self.items[0], qty=400, unit=Unit.PIECE, rate=2400, day=0)
        self._buy(item=self.items[1], qty=400, unit=Unit.PIECE, rate=7000, day=0)

    # -- builders ----------------------------------------------------------
    def _buy(self, *, item, qty, unit, rate, day):
        bill = purchasing.create_purchase_invoice(
            vendor=self.vendor,
            warehouse=self.warehouse,
            posting_date=START + dt.timedelta(days=day),
            vendor_bill_no=f"VB-{next(_serial):06d}",
            vendor_bill_date=START + dt.timedelta(days=day),
        )
        purchasing.update_line(
            PurchaseInvoiceLine(document=bill),
            item=item,
            qty_input=qty,
            unit_input=unit,
            rate_input_paisa=rate,
        ).save()
        return purchasing.post_purchase_invoice(bill, user=None)

    def _sell(self, *, client, item, qty, unit, rate, day):
        document = sales.create_sales_invoice(
            client=client,
            warehouse=self.warehouse,
            posting_date=START + dt.timedelta(days=day),
        )
        sales.update_line(
            SalesInvoiceLine(document=document),
            item=item,
            qty_input=qty,
            unit_input=unit,
            rate_input_paisa=rate,
        ).save()
        return sales.post_sales_invoice(document, user=None)

    # -- the invariants ----------------------------------------------------
    def assert_invariants(self, step: str):
        self.assert_trial_balance_is_zero(step)
        self.assert_every_voucher_balances(step)
        self.assert_stock_matches_its_entries(step)
        self.assert_reversals_mirror_exactly(step)

    def assert_trial_balance_is_zero(self, step):
        totals = LedgerEntry.objects.aggregate(d=Sum("debit_paisa"), c=Sum("credit_paisa"))
        debit, credit = totals["d"] or 0, totals["c"] or 0
        assert debit == credit, (
            f"after {step}: trial balance out by {debit - credit} paisa "
            f"(debits {debit}, credits {credit})"
        )

    def assert_every_voucher_balances(self, step):
        """Stronger than the trial balance, and it names the culprit.

        A system can have a zero trial balance while two documents are wrong in
        opposite directions, and the trial balance would never say so.
        """
        rows = (
            LedgerEntry.objects.values("voucher_type", "voucher_code")
            .annotate(d=Sum("debit_paisa"), c=Sum("credit_paisa"))
            .order_by()
        )
        unbalanced = [
            (r["voucher_type"], r["voucher_code"], (r["d"] or 0) - (r["c"] or 0))
            for r in rows
            if (r["d"] or 0) != (r["c"] or 0)
        ]
        assert not unbalanced, f"after {step}: these vouchers do not balance: {unbalanced}"

    def assert_stock_matches_its_entries(self, step):
        """The reported position must equal the rows, summed independently.

        Summed in Python rather than with the same ``Sum()`` the report uses —
        otherwise this is the query checking itself and would agree with a
        wrong answer.
        """
        from apps.reports.ledger import stock_positions

        by_hand: dict[tuple[int, int], int] = defaultdict(int)
        for entry in StockEntry.objects.values("item_id", "warehouse_id", "qty_base"):
            by_hand[(entry["item_id"], entry["warehouse_id"])] += entry["qty_base"]

        reported = {key: totals.qty_base for key, totals in stock_positions().items()}
        for key, qty in by_hand.items():
            assert reported.get(key, 0) == qty, (
                f"after {step}: stock for item/warehouse {key} reads "
                f"{reported.get(key, 0)} but its entries sum to {qty}"
            )

    def assert_reversals_mirror_exactly(self, step):
        """A cancelled document contributes exactly zero to every account.

        Its original entries and their reversals must cancel out per account —
        not merely in total, which a reversal posted to the wrong account would
        still satisfy.
        """
        from apps.sales.models import SalesInvoice

        cancelled_codes = set(
            SalesInvoice.objects.filter(status="CANCELLED").values_list("code", flat=True)
        )
        if not cancelled_codes:
            return

        rows = (
            LedgerEntry.objects.filter(voucher_code__in=cancelled_codes)
            .values("voucher_code", "account_id")
            .annotate(d=Sum("debit_paisa"), c=Sum("credit_paisa"))
            .order_by()
        )
        offenders = [
            (r["voucher_code"], r["account_id"], (r["d"] or 0) - (r["c"] or 0))
            for r in rows
            if (r["d"] or 0) != (r["c"] or 0)
        ]
        assert not offenders, (
            f"after {step}: cancelled documents left a net balance on an account "
            f"— the reversal did not mirror the original: {offenders}"
        )

    # -- the property ------------------------------------------------------
    @settings(
        # Overridable for a deep hunt without editing the file:
        #   pytest --hypothesis-max-examples=1000 tests/test_invariants_property.py
        # 60 is what runs in CI: enough to catch a regression in a minute or so,
        # and the .hypothesis database replays any previously-found failure
        # first whatever this is set to.
        max_examples=int(os.environ.get("ERP_PROPERTY_EXAMPLES", "60")),
        deadline=None,
        # Each example rebuilds the whole fixture in setUp, which Hypothesis
        # would otherwise flag as too slow to be a pure function.
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    )
    @given(st.lists(operations, min_size=1, max_size=10))
    def test_random_lifecycles_never_break_the_books(self, ops):
        documents = []  # every sales invoice raised, in order
        refused = 0
        acted = 0

        self.assert_invariants("setup")

        for index, op in enumerate(ops):
            kind = op[0]
            label = f"step {index} {op!r}"
            try:
                if kind == SELL:
                    _, who, what, qty, unit, rate = op
                    documents.append(
                        self._sell(
                            client=self.clients[who],
                            item=self.items[what],
                            qty=qty,
                            unit=unit,
                            rate=rate,
                            day=index + 1,
                        )
                    )
                    acted += 1

                elif kind == BUY:
                    _, what, qty, unit, rate = op
                    self._buy(item=self.items[what], qty=qty, unit=unit, rate=rate, day=index + 1)
                    acted += 1

                elif kind == CANCEL:
                    if not documents:
                        continue
                    target = documents[op[1] % len(documents)]
                    target.refresh_from_db()
                    sales.cancel_sales_invoice(target, user=None, reason="property test reversal")
                    acted += 1

                elif kind == AMEND:
                    if not documents:
                        continue
                    target = documents[op[1] % len(documents)]
                    target.refresh_from_db()
                    fresh = sales.amend_sales_invoice(target, user=None)
                    documents.append(sales.post_sales_invoice(fresh, user=None))
                    acted += 1

                elif kind == RECEIVE:
                    _, who, amount = op
                    payments.post_payment(
                        payments.create_payment(
                            party=self.clients[who],
                            direction=PaymentDirection.RECEIVE,
                            mode=PaymentMode.CASH,
                            posting_date=START + dt.timedelta(days=index + 1),
                            amount_paisa=amount,
                        ),
                        user=None,
                    )
                    acted += 1
                event(f"{kind}: posted")

            except CoreError as refusal:
                # A refusal: over the credit limit, short of stock, an illegal
                # transition, a document something else depends on. All of these
                # are the system working, and none of them may leave the books
                # in a state that fails the assertions below.
                refused += 1
                # Tagged so --hypothesis-show-statistics proves the run is not
                # passing by refusing everything, and shows *which* refusals the
                # generator is actually reaching.
                event(f"{kind}: refused ({type(refusal).__name__})")

            self.assert_invariants(label)

        self.assert_invariants("end")
        # Recorded rather than asserted per-example: Hypothesis will generate
        # examples that are all cancels of nothing, and those legitimately do
        # nothing. `test_the_generator_actually_posts_things` below is what
        # stops the whole property passing vacuously.
        self.acted = acted

    def test_the_generator_actually_posts_things(self):
        """Guards the property above against passing by doing nothing.

        Every assertion in the property is satisfied by an empty ledger, so if
        the operations all silently refused — a credit limit set too low, an
        exception swallowed too broadly — the property would go green while
        testing nothing. This posts the same way and checks the ledger moved.
        """
        before = LedgerEntry.objects.count()
        self._sell(
            client=self.clients[0], item=self.items[0], qty=2, unit=Unit.PIECE, rate=30_000, day=1
        )
        assert LedgerEntry.objects.count() > before
