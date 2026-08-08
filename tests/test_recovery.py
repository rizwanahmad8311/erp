"""Ageing, open items and the recovery workspace.

Four properties, and the first one is the reason the other three can be trusted:

* **The ladder ties out.** What a client owes, broken into buckets and netted
  against what is on account, is exactly their ledger balance. If those two ever
  disagree, one of them is lying and the accountant has no way to tell which.
* **The bucket boundaries are where everybody thinks they are.** 30, 60 and 90
  days are the three numbers arguments happen about, so all three are pinned
  from both sides.
* **Nothing here reads a document header.** Every amount comes from the ledger,
  grouped by voucher — which is what makes a cancelled invoice disappear from
  the sheet without anything having to know it was cancelled.
* **On-account money is visible.** Money that has arrived and settles nothing
  yet is a normal state, and a recovery sheet that hides it gets it collected
  twice.
"""

import datetime as dt

import pytest

from apps.accounting.enums import PartyType
from apps.accounting.services import party_balance
from apps.core.money import to_paisa
from apps.masters.enums import Unit
from apps.masters.models import Client, Item, Route, Seller, Vendor
from apps.payments import recovery, services
from apps.payments.enums import AgeingBucket, PaymentDirection, PaymentMode, bucket_for
from apps.purchasing import services as purchasing
from apps.sales import services as sales
from apps.sales.models import SalesInvoiceLine

pytestmark = pytest.mark.django_db

#: Everything below is measured against this day. Fixed, not "today", because a
#: suite that drifts with the calendar fails on one particular Tuesday a year.
TODAY = dt.date(2026, 6, 30)

BIG_LIMIT = to_paisa("10000000")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def routes(db):
    from types import SimpleNamespace

    return SimpleNamespace(
        city=Route.objects.create(code="R-01", name="Saddar & City"),
        north=Route.objects.create(code="R-02", name="North Nazimabad"),
    )


@pytest.fixture
def sellers(db):
    from types import SimpleNamespace

    return SimpleNamespace(
        imran=Seller.objects.create(code="S-01", name="Imran Qureshi"),
        bilal=Seller.objects.create(code="S-02", name="Bilal Ahmed"),
    )


@pytest.fixture
def shops(db, routes, sellers):
    from types import SimpleNamespace

    return SimpleNamespace(
        madina=Client.objects.create(
            code="C-0001",
            name="Al-Madina Kiryana",
            phone="0300-2214477",
            route=routes.city,
            seller=sellers.imran,
            credit_limit_paisa=BIG_LIMIT,
            credit_days=0,
        ),
        nazim=Client.objects.create(
            code="C-0002",
            name="Nazimabad General Store",
            phone="0321-9988776",
            route=routes.north,
            seller=sellers.bilal,
            credit_limit_paisa=BIG_LIMIT,
            credit_days=0,
        ),
    )


@pytest.fixture
def oil(db):
    return Item.objects.create(code="OIL-1000", name="Cooking Oil 1L", carton_size=12)


@pytest.fixture
def stocked(db, accounts, warehouses, oil, user):
    """Plenty of stock, received long before any invoice below is dated.

    Back-dated on purpose: the ageing tests post invoices up to a year old, and
    the stock ledger values an issue against the position *as it stood on the
    posting date* — so goods received last week cannot be sold last year.
    """
    vendor = Vendor.objects.create(code="V-01", name="Dalda Foods")
    bill = purchasing.create_purchase_invoice(
        vendor=vendor, warehouse=warehouses.main, posting_date=dt.date(2024, 1, 1)
    )
    line = purchasing.update_line(
        purchasing.PurchaseInvoiceLine(document=bill),
        item=oil,
        qty_input=1000,
        unit_input=Unit.CARTON,
        rate_input_paisa=to_paisa("2400"),
    )
    line.save()
    purchasing.post_purchase_invoice(bill, user=user)
    return bill


def bill(shop, warehouse, oil, user, *, on, rupees="1000", cartons=1, due_date=None):
    """A posted sales invoice for a round number of rupees."""
    invoice = sales.create_sales_invoice(
        client=shop, warehouse=warehouse, posting_date=on, due_date=due_date
    )
    line = sales.update_line(
        SalesInvoiceLine(document=invoice),
        item=oil,
        qty_input=cartons,
        unit_input=Unit.CARTON,
        rate_input_paisa=to_paisa(rupees),
    )
    line.save()
    return sales.post_sales_invoice(invoice, user=user)


def take(shop, *, rupees, on, mode=PaymentMode.CASH, user=None, **fields):
    payment = services.create_payment(
        party=shop,
        direction=PaymentDirection.RECEIVE,
        mode=mode,
        posting_date=on,
        amount_paisa=to_paisa(rupees),
        **fields,
    )
    return services.post_payment(payment, user=user)


def days_before(n: int) -> dt.date:
    return TODAY - dt.timedelta(days=n)


# ===========================================================================
# The ladder itself
# ===========================================================================
class TestBucketBoundaries:
    """The three numbers everybody argues about, pinned from both sides."""

    @pytest.mark.parametrize(
        ("days", "expected"),
        [
            (-10, AgeingBucket.CURRENT),
            (-1, AgeingBucket.CURRENT),
            (0, AgeingBucket.CURRENT),
            (1, AgeingBucket.DAYS_1_30),
            (29, AgeingBucket.DAYS_1_30),
            (30, AgeingBucket.DAYS_1_30),
            (31, AgeingBucket.DAYS_31_60),
            (59, AgeingBucket.DAYS_31_60),
            (60, AgeingBucket.DAYS_31_60),
            (61, AgeingBucket.DAYS_61_90),
            (89, AgeingBucket.DAYS_61_90),
            (90, AgeingBucket.DAYS_61_90),
            (91, AgeingBucket.DAYS_90_PLUS),
            (365, AgeingBucket.DAYS_90_PLUS),
        ],
    )
    def test_bucket_for(self, days, expected):
        assert bucket_for(days) == expected

    def test_due_today_is_current_not_overdue(self):
        """Day zero is the last day to pay, not the first day of being late."""
        assert bucket_for(0) == AgeingBucket.CURRENT


class TestAgeingThroughTheLedger:
    """The same boundaries, reached the long way — through real invoices."""

    @pytest.mark.parametrize(
        ("days", "expected"),
        [
            (30, AgeingBucket.DAYS_1_30),
            (31, AgeingBucket.DAYS_31_60),
            (60, AgeingBucket.DAYS_31_60),
            (61, AgeingBucket.DAYS_61_90),
            (90, AgeingBucket.DAYS_61_90),
            (91, AgeingBucket.DAYS_90_PLUS),
        ],
    )
    def test_an_invoice_lands_in_the_right_bucket(
        self, shops, warehouses, oil, stocked, user, days, expected
    ):
        bill(shops.madina, warehouses.main, oil, user, on=days_before(days), rupees="1000")

        row = recovery.client_recovery(shops.madina, as_of=TODAY)

        assert len(row.open_items) == 1
        assert row.open_items[0].days_overdue == days
        assert row.open_items[0].bucket == expected
        assert row.buckets[expected] == to_paisa("1000")

    def test_ageing_is_measured_from_the_due_date_not_the_invoice_date(
        self, shops, warehouses, oil, stocked, user
    ):
        """A shop on 15 days' credit is not overdue on day 14."""
        shops.madina.credit_days = 15
        shops.madina.save()
        invoice = bill(shops.madina, warehouses.main, oil, user, on=days_before(14))

        assert invoice.due_date == days_before(-1)
        row = recovery.client_recovery(shops.madina, as_of=TODAY)
        assert row.open_items[0].bucket == AgeingBucket.CURRENT
        assert row.overdue_paisa == 0

    def test_a_document_without_a_due_date_ages_from_its_posting_date(
        self, shops, warehouses, oil, stocked, user
    ):
        """A supplier bill and a credit note have no due date. They age anyway.

        Falling back to the posting date is what stops a document with no due
        date sitting at the top of the sheet forever, permanently current.
        """
        invoice = bill(shops.madina, warehouses.main, oil, user, on=days_before(45))
        invoice.due_date = None  # in memory only; the posted row is frozen

        assert recovery.document_due_date(invoice) == days_before(45)

    def test_the_buckets_split_a_client_across_bands(self, shops, warehouses, oil, stocked, user):
        bill(shops.madina, warehouses.main, oil, user, on=days_before(5), rupees="1000")
        bill(shops.madina, warehouses.main, oil, user, on=days_before(45), rupees="2000")
        bill(shops.madina, warehouses.main, oil, user, on=days_before(120), rupees="3000")

        row = recovery.client_recovery(shops.madina, as_of=TODAY)

        assert row.buckets[AgeingBucket.DAYS_1_30] == to_paisa("1000")
        assert row.buckets[AgeingBucket.DAYS_31_60] == to_paisa("2000")
        assert row.buckets[AgeingBucket.DAYS_90_PLUS] == to_paisa("3000")
        assert row.buckets[AgeingBucket.CURRENT] == 0
        assert row.overdue_paisa == to_paisa("6000")
        assert row.worst_bucket == AgeingBucket.DAYS_90_PLUS


# ===========================================================================
# The invariant everything else rests on
# ===========================================================================
class TestTiesOut:
    def test_the_ladder_adds_up_to_the_ledger(self, shops, warehouses, oil, stocked, user):
        bill(shops.madina, warehouses.main, oil, user, on=days_before(70), rupees="5000")
        bill(shops.madina, warehouses.main, oil, user, on=days_before(10), rupees="3000")
        payment = take(shops.madina, rupees="4000", on=days_before(5), user=user)
        services.allocate_payment(payment, [(_oldest(shops.madina), to_paisa("4000"))])

        row = recovery.client_recovery(shops.madina, as_of=TODAY)

        assert row.ties_out()
        assert row.outstanding_paisa == party_balance(PartyType.CLIENT, shops.madina.pk).paisa
        assert row.open_paisa - row.on_account_paisa == row.outstanding_paisa

    def test_it_still_ties_out_with_money_on_account(self, shops, warehouses, oil, stocked, user):
        bill(shops.madina, warehouses.main, oil, user, on=days_before(10), rupees="3000")
        take(shops.madina, rupees="8000", on=days_before(2), user=user)

        row = recovery.client_recovery(shops.madina, as_of=TODAY)

        assert row.open_paisa == to_paisa("3000")
        assert row.on_account_paisa == to_paisa("8000")
        assert row.outstanding_paisa == to_paisa("-5000")
        assert row.ties_out()

    def test_it_still_ties_out_with_a_credit_note(self, shops, warehouses, oil, stocked, user):
        from apps.sales.models import SalesReturnLine

        invoice = bill(shops.madina, warehouses.main, oil, user, on=days_before(20), rupees="5000")
        note = sales.create_sales_return(
            client=shops.madina,
            warehouse=warehouses.main,
            posting_date=days_before(10),
            against_invoice=invoice,
        )
        line = sales.update_line(
            SalesReturnLine(document=note),
            item=oil,
            qty_input=1,
            unit_input=Unit.CARTON,
            rate_input_paisa=to_paisa("1000"),
        )
        line.save()
        sales.post_sales_return(note, user=user)

        row = recovery.client_recovery(shops.madina, as_of=TODAY)

        assert row.open_paisa == to_paisa("5000")
        assert row.on_account_paisa == to_paisa("1000")
        assert row.ties_out()

    def test_a_cancelled_invoice_leaves_the_sheet_by_itself(
        self, shops, warehouses, oil, stocked, user
    ):
        """Nothing filters on status. The reversing rows net to zero and it goes."""
        invoice = bill(shops.madina, warehouses.main, oil, user, on=days_before(20), rupees="5000")
        bill(shops.madina, warehouses.main, oil, user, on=days_before(10), rupees="3000")

        sales.cancel_sales_invoice(invoice, user=user, reason="Wrong shop")

        row = recovery.client_recovery(shops.madina, as_of=TODAY)
        assert [item.voucher_code for item in row.open_items] != [invoice.code]
        assert row.open_paisa == to_paisa("3000")
        assert row.ties_out()

    def test_a_part_paid_invoice_shows_only_what_is_left(
        self, shops, warehouses, oil, stocked, user
    ):
        invoice = bill(shops.madina, warehouses.main, oil, user, on=days_before(40), rupees="5000")
        payment = take(shops.madina, rupees="2000", on=days_before(5), user=user)
        services.allocate_payment(payment, [(invoice, to_paisa("2000"))])

        row = recovery.client_recovery(shops.madina, as_of=TODAY)
        item = row.open_items[0]

        assert item.original_paisa == to_paisa("5000")
        assert item.allocated_paisa == to_paisa("2000")
        assert item.outstanding_paisa == to_paisa("3000")
        assert item.is_part_paid
        assert row.on_account_paisa == 0
        assert row.ties_out()


def _oldest(shop):
    """The client's oldest open invoice, as the workspace's own view sees it."""
    items, _credits = recovery.open_items(PartyType.CLIENT, shop.pk, as_of=TODAY)
    document = items[0].document()
    return document


# ===========================================================================
# The workspace row
# ===========================================================================
class TestWorkspaceRows:
    def test_a_row_carries_what_the_screen_shows(self, shops, warehouses, oil, stocked, user):
        bill(shops.madina, warehouses.main, oil, user, on=days_before(70), rupees="5000")
        bill(shops.madina, warehouses.main, oil, user, on=days_before(20), rupees="3000")
        take(shops.madina, rupees="1000", on=days_before(3), user=user)

        (row,) = recovery.recovery_rows(as_of=TODAY, route=shops.madina.route)

        assert row.client == shops.madina
        assert row.phone == "0300-2214477"
        assert row.outstanding_paisa == to_paisa("7000")
        assert row.oldest_invoice_date == days_before(70)
        assert row.oldest_days == 70
        assert row.last_payment_date == days_before(3)
        assert row.last_payment_paisa == to_paisa("1000")
        assert row.last_payment_code.startswith("RV-")

    def test_every_row_on_the_sheet_ties_out_to_the_ledger(
        self, shops, warehouses, oil, stocked, user
    ):
        """The same invariant as :class:`TestTiesOut`, on the workspace's own rows.

        Worth pinning separately: ``recovery_rows`` builds the balance from the
        raw ledger vouchers while ``client_recovery`` reads ``party_balance``,
        so the two are genuinely different arithmetic arriving at one number. A
        part-paid invoice is the case that separates them — the invoice stays on
        the sheet and the receipt that settled part of it drops off, and a
        balance summed from what is left over-states the debt by the payment.
        """
        first = bill(shops.madina, warehouses.main, oil, user, on=days_before(40), rupees="5000")
        bill(shops.madina, warehouses.main, oil, user, on=days_before(5), rupees="2000")
        payment = take(shops.madina, rupees="2000", on=days_before(3), user=user)
        services.allocate_payment(payment, [(first, to_paisa("2000"))])
        bill(shops.nazim, warehouses.main, oil, user, on=days_before(200), rupees="4000")

        for row in recovery.recovery_rows(as_of=TODAY):
            assert row.ties_out(), f"{row.client.code} does not tie out"
            assert (
                row.outstanding_paisa == party_balance(PartyType.CLIENT, row.client.pk, TODAY).paisa
            )

    def test_a_settled_client_is_off_the_sheet(self, shops, warehouses, oil, stocked, user):
        invoice = bill(shops.madina, warehouses.main, oil, user, on=days_before(10), rupees="1000")
        payment = take(shops.madina, rupees="1000", on=days_before(1), user=user)
        services.allocate_payment(payment, [(invoice, to_paisa("1000"))])

        assert recovery.recovery_rows(as_of=TODAY) == []

    def test_a_client_with_only_money_on_account_stays_on_the_sheet(
        self, shops, warehouses, oil, stocked, user
    ):
        """Because that money has to be applied to something by somebody."""
        take(shops.madina, rupees="4000", on=days_before(1), user=user)

        (row,) = recovery.recovery_rows(as_of=TODAY)

        assert row.open_items == ()
        assert row.on_account_paisa == to_paisa("4000")
        assert row.has_on_account
        assert row.outstanding_paisa == to_paisa("-4000")

    def test_rows_come_back_oldest_money_first(self, shops, warehouses, oil, stocked, user):
        bill(shops.madina, warehouses.main, oil, user, on=days_before(5), rupees="9000")
        bill(shops.nazim, warehouses.main, oil, user, on=days_before(200), rupees="1000")

        rows = recovery.recovery_rows(as_of=TODAY)

        assert [row.client.code for row in rows] == ["C-0002", "C-0001"]

    def test_the_summary_strip_totals_every_bucket(self, shops, warehouses, oil, stocked, user):
        bill(shops.madina, warehouses.main, oil, user, on=days_before(10), rupees="1000")
        bill(shops.nazim, warehouses.main, oil, user, on=days_before(20), rupees="2000")
        bill(shops.nazim, warehouses.main, oil, user, on=days_before(200), rupees="4000")

        summary = {
            bucket: paisa
            for bucket, _label, paisa, _alarm in recovery.ageing_summary(
                recovery.recovery_rows(as_of=TODAY)
            )
        }

        assert summary[AgeingBucket.DAYS_1_30] == to_paisa("3000")
        assert summary[AgeingBucket.DAYS_90_PLUS] == to_paisa("4000")
        assert summary[AgeingBucket.CURRENT] == 0


class TestFilters:
    @pytest.fixture(autouse=True)
    def _books(self, shops, warehouses, oil, stocked, user):
        bill(shops.madina, warehouses.main, oil, user, on=days_before(10), rupees="1000")
        bill(shops.nazim, warehouses.main, oil, user, on=days_before(200), rupees="4000")

    def test_by_route(self, shops, routes):
        rows = recovery.recovery_rows(as_of=TODAY, route=routes.north)
        assert [row.client.code for row in rows] == ["C-0002"]

    def test_by_seller(self, shops, sellers):
        rows = recovery.recovery_rows(as_of=TODAY, seller=sellers.imran)
        assert [row.client.code for row in rows] == ["C-0001"]

    def test_by_client_name(self, shops):
        rows = recovery.recovery_rows(as_of=TODAY, query="nazimabad")
        assert [row.client.code for row in rows] == ["C-0002"]

    def test_by_phone(self, shops):
        """How a shopkeeper on the line identifies themselves."""
        rows = recovery.recovery_rows(as_of=TODAY, query="2214477")
        assert [row.client.code for row in rows] == ["C-0001"]

    def test_by_bucket(self, shops):
        rows = recovery.recovery_rows(as_of=TODAY, bucket=AgeingBucket.DAYS_90_PLUS)
        assert [row.client.code for row in rows] == ["C-0002"]

        rows = recovery.recovery_rows(as_of=TODAY, bucket=AgeingBucket.DAYS_1_30)
        assert [row.client.code for row in rows] == ["C-0001"]

    def test_as_of_looks_backwards(self, shops):
        """The sheet as it stood a fortnight ago, not the sheet with old dates."""
        rows = recovery.recovery_rows(as_of=days_before(20))
        assert [row.client.code for row in rows] == ["C-0002"]


# ===========================================================================
# Today's recovery
# ===========================================================================
class TestTodaysRecovery:
    def test_collected_against_outstanding_per_route(
        self, shops, routes, warehouses, oil, stocked, user
    ):
        bill(shops.madina, warehouses.main, oil, user, on=days_before(30), rupees="10000")
        bill(shops.nazim, warehouses.main, oil, user, on=days_before(30), rupees="10000")
        take(shops.madina, rupees="4000", on=TODAY, user=user)

        lines = {line.route_code: line for line in recovery.todays_recovery(on=TODAY)}

        assert lines["R-01"].collected_paisa == to_paisa("4000")
        assert lines["R-01"].payment_count == 1
        assert lines["R-01"].outstanding_paisa == to_paisa("6000")
        assert lines["R-02"].collected_paisa == 0
        assert lines["R-02"].outstanding_paisa == to_paisa("10000")

    def test_the_day_totals(self, shops, warehouses, oil, stocked, user):
        bill(shops.madina, warehouses.main, oil, user, on=days_before(30), rupees="10000")
        take(shops.madina, rupees="4000", on=TODAY, user=user)

        collected, outstanding, count = recovery.day_totals(recovery.todays_recovery(on=TODAY))

        assert (collected, outstanding, count) == (to_paisa("4000"), to_paisa("6000"), 1)

    def test_a_cheque_in_the_drawer_counts_as_collected(
        self, shops, warehouses, oil, stocked, user
    ):
        """It was collected. It is in the drawer. The accountant needs to see it."""
        bill(shops.madina, warehouses.main, oil, user, on=days_before(30), rupees="10000")
        take(
            shops.madina,
            rupees="4000",
            on=TODAY,
            mode=PaymentMode.CHEQUE,
            cheque_no="00123",
            cheque_date=TODAY + dt.timedelta(days=21),
            user=user,
        )

        (line,) = recovery.todays_recovery(on=TODAY)
        assert line.collected_paisa == to_paisa("4000")

    def test_a_bounced_cheque_was_never_recovery(self, shops, warehouses, oil, stocked, user):
        bill(shops.madina, warehouses.main, oil, user, on=days_before(30), rupees="10000")
        payment = take(
            shops.madina,
            rupees="4000",
            on=TODAY,
            mode=PaymentMode.CHEQUE,
            cheque_no="00123",
            cheque_date=TODAY,
            user=user,
        )
        services.bounce_cheque(payment, posting_date=TODAY, user=user)

        (line,) = recovery.todays_recovery(on=TODAY)
        assert line.collected_paisa == 0
        assert line.outstanding_paisa == to_paisa("10000")

    def test_the_recovery_rate_is_integer_arithmetic(self, shops, warehouses, oil, stocked, user):
        bill(shops.madina, warehouses.main, oil, user, on=days_before(30), rupees="10000")
        take(shops.madina, rupees="2500", on=TODAY, user=user)

        (line,) = recovery.todays_recovery(on=TODAY)
        assert line.recovery_rate_bp == 2500
        assert line.recovery_rate_display == "25.00%"


# ===========================================================================
# The bounced-cheque flag
# ===========================================================================
class TestFlag:
    def test_a_bounce_flags_the_shop(self, shops, warehouses, oil, stocked, user):
        bill(shops.madina, warehouses.main, oil, user, on=days_before(30), rupees="10000")
        payment = take(
            shops.madina,
            rupees="4000",
            on=days_before(20),
            mode=PaymentMode.CHEQUE,
            cheque_no="00123",
            cheque_date=days_before(10),
            user=user,
        )
        services.bounce_cheque(payment, posting_date=days_before(5), user=user)

        row = recovery.client_recovery(shops.madina, as_of=TODAY)

        assert row.is_flagged
        assert row.bounced_cheque_count == 1
        assert row.bounced_cheque_paisa == to_paisa("4000")

    def test_a_shop_that_has_never_bounced_one_is_not_flagged(
        self, shops, warehouses, oil, stocked, user
    ):
        bill(shops.madina, warehouses.main, oil, user, on=days_before(30), rupees="10000")
        row = recovery.client_recovery(shops.madina, as_of=TODAY)
        assert not row.is_flagged

    def test_cancelling_the_bounce_clears_the_flag(self, shops, warehouses, oil, stocked, user):
        """Because the flag is the event, not a column somebody has to unset."""
        bill(shops.madina, warehouses.main, oil, user, on=days_before(30), rupees="10000")
        payment = take(
            shops.madina,
            rupees="4000",
            on=days_before(20),
            mode=PaymentMode.CHEQUE,
            cheque_no="00123",
            cheque_date=days_before(10),
            user=user,
        )
        event = services.bounce_cheque(payment, posting_date=days_before(5), user=user)
        services.cancel_cheque_event(event, user=user, reason="Bank had it wrong")

        row = recovery.client_recovery(shops.madina, as_of=TODAY)
        assert not row.is_flagged


# ===========================================================================
# The cheque register
# ===========================================================================
class TestPendingCheques:
    def test_the_drawer_lists_what_has_not_cleared(self, shops, warehouses, oil, stocked, user):
        bill(shops.madina, warehouses.main, oil, user, on=days_before(40), rupees="20000")
        early = take(
            shops.madina,
            rupees="5000",
            on=days_before(30),
            mode=PaymentMode.CHEQUE,
            cheque_no="00001",
            cheque_date=days_before(1),
            user=user,
        )
        late = take(
            shops.madina,
            rupees="5000",
            on=days_before(20),
            mode=PaymentMode.CHEQUE,
            cheque_no="00002",
            cheque_date=TODAY + dt.timedelta(days=30),
            user=user,
        )
        cleared = take(
            shops.madina,
            rupees="5000",
            on=days_before(25),
            mode=PaymentMode.CHEQUE,
            cheque_no="00003",
            cheque_date=days_before(5),
            user=user,
        )
        services.clear_cheque(cleared, posting_date=days_before(4), user=user)

        drawer = list(recovery.pending_cheques(as_of=TODAY))

        assert [payment.pk for payment in drawer] == [early.pk, late.pk]

    def test_the_drawer_matches_cheques_in_hand(
        self, accounts, shops, warehouses, oil, stocked, user
    ):
        """A reconciliation somebody can do by eye, which is the point of it."""
        from apps.accounting import chart as coa
        from apps.accounting.services import account_balance

        bill(shops.madina, warehouses.main, oil, user, on=days_before(40), rupees="20000")
        for number, rupees in (("00001", "5000"), ("00002", "3000")):
            take(
                shops.madina,
                rupees=rupees,
                on=days_before(20),
                mode=PaymentMode.CHEQUE,
                cheque_no=number,
                cheque_date=days_before(1),
                user=user,
            )

        drawer = sum(payment.amount_paisa for payment in recovery.pending_cheques(as_of=TODAY))
        assert drawer == account_balance(accounts.by_code[coa.CHEQUES_IN_HAND]).paisa
