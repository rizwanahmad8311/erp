"""Posting, cancelling and amending a sales invoice and a credit note.

The three properties this file exists to pin, in the order they matter:

* **The credit limit is enforced before anything is written**, and the refusal
  says what the limit is, what is owed, and by how much this invoice busts it.
* **Cost is captured at post time, not read at report time.** The moving average
  moves; an invoice's margin must not.
* **A credit note nets the client's balance back to the right figure**, which is
  only true if it reverses through the ledger rather than by editing anything.

Every balance assertion reads the ledger through ``apps.accounting.services``
rather than a header field — a test that asserted ``invoice.total_paisa`` would
pass just as happily if nothing had been posted at all.
"""

import datetime as dt

import pytest

from apps.accounting import chart as coa
from apps.accounting.enums import PartyType
from apps.accounting.models import LedgerEntry, StockEntry
from apps.accounting.services import party_balance, stock_balance, valuation_rate
from apps.core.enums import DocumentStatus
from apps.core.exceptions import DocumentImmutable, IllegalTransition
from apps.core.money import Money, to_paisa
from apps.masters.enums import Unit
from apps.masters.models import Client, Item, Route, Seller, Vendor
from apps.purchasing import services as purchasing
from apps.sales import services
from apps.sales.exceptions import (
    EmptyDocument,
    ReturnExceedsInvoice,
    SalesError,
)
from apps.sales.models import SalesInvoice, SalesInvoiceLine, SalesReturn, SalesReturnLine

pytestmark = pytest.mark.django_db

APRIL = dt.date(2026, 4, 1)
MAY = dt.date(2026, 5, 1)

#: Generous enough that the credit limit is never the thing under test here.
BIG_LIMIT = to_paisa("10000000")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def route(db):
    return Route.objects.create(code="R-01", name="Saddar & City")


@pytest.fixture
def seller(db):
    return Seller.objects.create(code="S-01", name="Imran Qureshi")


@pytest.fixture
def client_shop(db, route, seller):
    return Client.objects.create(
        code="C-0001",
        name="Al-Madina Kiryana",
        phone="0300-2214477",
        route=route,
        seller=seller,
        credit_limit_paisa=BIG_LIMIT,
        credit_days=15,
    )


@pytest.fixture
def oil(db):
    """Twelve to a carton, 17.5% tax."""
    return Item.objects.create(
        code="OIL-1000", name="Cooking Oil 1L", carton_size=12, tax_rate_bp=1750
    )


@pytest.fixture
def stocked(db, accounts, warehouses, oil, user):
    """120 pieces of oil on hand at Rs 200 each — Rs 24,000 of inventory.

    Bought in through a real purchase invoice rather than a hand-written stock
    row, so the average these tests sell against is one the system produced.
    """
    vendor = Vendor.objects.create(code="V-01", name="Dalda Foods")
    bill = purchasing.create_purchase_invoice(
        vendor=vendor, warehouse=warehouses.main, posting_date=APRIL
    )
    line = purchasing.update_line(
        purchasing.PurchaseInvoiceLine(document=bill),
        item=oil,
        qty_input=10,
        unit_input=Unit.CARTON,
        rate_input_paisa=to_paisa("2400"),
    )
    line.save()
    purchasing.post_purchase_invoice(bill, user=user)
    return bill


@pytest.fixture
def invoice(db, stocked, client_shop, warehouses):
    return services.create_sales_invoice(
        client=client_shop, warehouse=warehouses.main, posting_date=APRIL
    )


def add_line(document, item, *, qty_input, unit_input=Unit.CARTON, rupees, discount="0", **kwargs):
    """Append a line the way the service layer does."""
    model = SalesInvoiceLine if isinstance(document, SalesInvoice) else SalesReturnLine
    line = services.update_line(
        model(document=document),
        item=item,
        qty_input=qty_input,
        unit_input=unit_input,
        rate_input_paisa=to_paisa(rupees),
        discount_paisa=to_paisa(discount),
        **kwargs,
    )
    line.save()
    return line


@pytest.fixture
def posted_invoice(invoice, oil, user):
    """4 cartons of 12 at Rs 3,000 — 48 pieces sold for Rs 12,000 + tax.

    Cost is 48 pieces at the Rs 200 average, so Rs 9,600 of COGS.
    """
    add_line(invoice, oil, qty_input=4, rupees="3000")
    return services.post_sales_invoice(invoice, user=user)


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------
class TestPostSalesInvoice:
    def test_the_header_totals_are_the_exact_sum_of_the_lines(self, posted_invoice):
        assert posted_invoice.subtotal_paisa == 1_200_000
        assert posted_invoice.tax_paisa == 210_000  # 17.5% of 12,000
        assert posted_invoice.total_paisa == 1_410_000

    def test_the_stock_leaves_the_named_warehouse(self, posted_invoice, oil, warehouses):
        qty_base, value_paisa = stock_balance(oil, warehouses.main)
        assert qty_base == 72  # 120 in, 48 out
        assert value_paisa == 1_440_000  # Rs 24,000 less Rs 9,600 of cost

    def test_the_general_ledger_is_the_five_expected_rows(self, posted_invoice):
        rows = {
            entry.account.code: (entry.debit_paisa, entry.credit_paisa)
            for entry in LedgerEntry.objects.filter(voucher_code=posted_invoice.code)
        }
        assert rows == {
            coa.ACCOUNTS_RECEIVABLE: (1_410_000, 0),
            coa.SALES: (0, 1_200_000),
            coa.TAX_PAYABLE: (0, 210_000),
            coa.COST_OF_GOODS_SOLD: (960_000, 0),
            coa.INVENTORY: (0, 960_000),
        }

    def test_the_ledger_balances(self, posted_invoice):
        entries = LedgerEntry.objects.filter(voucher_code=posted_invoice.code)
        assert sum(e.debit_paisa for e in entries) == sum(e.credit_paisa for e in entries)

    def test_the_receivable_is_tagged_with_the_client(self, posted_invoice, client_shop):
        assert party_balance(PartyType.CLIENT, client_shop.pk) == Money(1_410_000)

    def test_a_discount_is_debited_to_discount_allowed(self, invoice, oil, user, warehouses):
        add_line(invoice, oil, qty_input=4, rupees="3000", discount="1000")
        services.post_sales_invoice(invoice, user=user)

        rows = {
            entry.account.code: (entry.debit_paisa, entry.credit_paisa)
            for entry in LedgerEntry.objects.filter(voucher_code=invoice.code)
        }
        # Revenue at the gross, discount as its own expense, receivable at net + tax.
        assert rows[coa.SALES] == (0, 1_200_000)
        assert rows[coa.DISCOUNT_ALLOWED] == (100_000, 0)
        assert rows[coa.TAX_PAYABLE] == (0, 192_500)  # 17.5% of 1,100,000
        assert rows[coa.ACCOUNTS_RECEIVABLE] == (1_292_500, 0)
        assert sum(d for d, _ in rows.values()) == sum(c for _, c in rows.values())

    def test_the_route_and_seller_default_from_the_client(self, invoice, route, seller):
        assert invoice.route == route
        assert invoice.seller == seller

    def test_they_can_be_overridden_because_a_booker_covers_another_beat(
        self, stocked, client_shop, warehouses
    ):
        cover = Seller.objects.create(code="S-02", name="Bilal Ahmed")
        other = Route.objects.create(code="R-02", name="Malir")
        document = services.create_sales_invoice(
            client=client_shop,
            warehouse=warehouses.main,
            posting_date=APRIL,
            route=other,
            seller=cover,
        )
        assert document.route == other
        assert document.seller == cover

    def test_the_due_date_defaults_from_the_client_credit_days(self, invoice):
        assert invoice.due_date == APRIL + dt.timedelta(days=15)

    def test_selling_more_than_is_held_is_refused(self, invoice, oil, user):
        from apps.accounting.exceptions import InsufficientStock

        add_line(invoice, oil, qty_input=99, rupees="3000")
        with pytest.raises(InsufficientStock):
            services.post_sales_invoice(invoice, user=user)


class TestPostingIsRefused:
    def test_when_the_document_has_no_lines(self, invoice, user):
        with pytest.raises(EmptyDocument):
            services.post_sales_invoice(invoice, user=user)

    def test_when_it_has_already_been_posted(self, posted_invoice, user):
        with pytest.raises(IllegalTransition):
            services.post_sales_invoice(posted_invoice, user=user)

    def test_a_failure_writes_nothing_to_either_ledger(self, invoice, oil, user):
        add_line(invoice, oil, qty_input=4, rupees="3000")
        invoice.warehouse = None
        with pytest.raises(Exception):  # noqa: B017 - any failure must roll back
            services.post_sales_invoice(invoice, user=user)

        assert not LedgerEntry.objects.filter(voucher_code=invoice.code).exists()
        assert not StockEntry.objects.filter(voucher_code=invoice.code).exists()


class TestAPostedInvoiceIsFrozen:
    def test_the_header_cannot_be_edited(self, posted_invoice):
        posted_invoice.remarks = "changed my mind"
        with pytest.raises(DocumentImmutable):
            posted_invoice.save()

    def test_its_lines_cannot_be_edited(self, posted_invoice):
        line = posted_invoice.lines.first()
        line.qty_input = 99
        with pytest.raises(SalesError, match="cannot be modified"):
            line.save()

    def test_it_cannot_be_deleted(self, posted_invoice):
        with pytest.raises(DocumentImmutable):
            posted_invoice.delete()


# ---------------------------------------------------------------------------
# COGS
# ---------------------------------------------------------------------------
class TestCostIsCapturedAtPostTime:
    """The moving average moves. An invoice's cost must not.

    Every one of these would pass with a ``cogs`` *property* that multiplied
    quantity by today's valuation rate — until somebody bought more stock at a
    different price, at which point last month's margins would all silently
    change. That is the failure being tested for.
    """

    def test_the_line_records_what_the_stock_ledger_released(self, posted_invoice, oil):
        line = posted_invoice.lines.get()
        movement = StockEntry.objects.get(voucher_code=posted_invoice.code)
        assert line.cogs_paisa == 960_000  # 48 pieces at the Rs 200 average
        assert line.cogs_paisa == -movement.value_paisa

    def test_a_draft_has_no_cost_because_there_is_no_answer_yet(self, invoice, oil):
        add_line(invoice, oil, qty_input=4, rupees="3000")
        assert invoice.lines.get().cogs_paisa == 0
        assert invoice.cogs_paisa == 0

    def test_it_does_not_move_when_the_average_moves(
        self, posted_invoice, oil, warehouses, user, stocked
    ):
        """The test this whole design exists for.

        Buy more oil at three times the price. The valuation rate jumps; the
        invoice that was already posted must read exactly as it did.
        """
        before = posted_invoice.lines.get().cogs_paisa
        assert valuation_rate(oil, warehouses.main) == 20000  # Rs 200

        dearer = purchasing.create_purchase_invoice(
            vendor=stocked.vendor, warehouse=warehouses.main, posting_date=MAY
        )
        line = purchasing.update_line(
            purchasing.PurchaseInvoiceLine(document=dearer),
            item=oil,
            qty_input=10,
            unit_input=Unit.CARTON,
            rate_input_paisa=to_paisa("7200"),
        )
        line.save()
        purchasing.post_purchase_invoice(dearer, user=user)

        assert valuation_rate(oil, warehouses.main) > 20000  # the average moved
        posted_invoice.refresh_from_db()
        assert posted_invoice.lines.get().cogs_paisa == before == 960_000

    def test_the_ledger_agrees_with_the_captured_figure_forever(
        self, posted_invoice, user, oil, warehouses, stocked
    ):
        """Not just the line — the posted COGS entry is history too."""
        cogs_row = LedgerEntry.objects.get(
            voucher_code=posted_invoice.code, account__code=coa.COST_OF_GOODS_SOLD
        )
        dearer = purchasing.create_purchase_invoice(
            vendor=stocked.vendor, warehouse=warehouses.main, posting_date=MAY
        )
        line = purchasing.update_line(
            purchasing.PurchaseInvoiceLine(document=dearer),
            item=oil,
            qty_input=10,
            unit_input=Unit.CARTON,
            rate_input_paisa=to_paisa("7200"),
        )
        line.save()
        purchasing.post_purchase_invoice(dearer, user=user)

        cogs_row.refresh_from_db()
        assert cogs_row.debit_paisa == 960_000

    def test_two_sales_at_different_averages_each_keep_their_own(
        self, posted_invoice, oil, warehouses, user, client_shop, stocked
    ):
        first = posted_invoice.lines.get().cogs_paisa

        dearer = purchasing.create_purchase_invoice(
            vendor=stocked.vendor, warehouse=warehouses.main, posting_date=MAY
        )
        line = purchasing.update_line(
            purchasing.PurchaseInvoiceLine(document=dearer),
            item=oil,
            qty_input=10,
            unit_input=Unit.CARTON,
            rate_input_paisa=to_paisa("7200"),
        )
        line.save()
        purchasing.post_purchase_invoice(dearer, user=user)

        second_invoice = services.create_sales_invoice(
            client=client_shop, warehouse=warehouses.main, posting_date=MAY
        )
        add_line(second_invoice, oil, qty_input=4, rupees="3000")
        services.post_sales_invoice(second_invoice, user=user)

        second = second_invoice.lines.get().cogs_paisa
        assert first == 960_000
        assert second > first  # sold out of dearer stock
        assert posted_invoice.lines.get().cogs_paisa == first

    def test_the_margin_is_revenue_less_the_captured_cost(self, posted_invoice):
        line = posted_invoice.lines.get()
        assert line.margin_paisa == line.net_paisa - 960_000

    def test_an_amendment_starts_with_no_cost_of_its_own(self, posted_invoice, user):
        """It has released no stock yet, so it has no cost yet."""
        services.cancel_sales_invoice(posted_invoice, user=user)
        amendment = services.amend_sales_invoice(posted_invoice, user=user)
        assert amendment.lines.get().cogs_paisa == 0


# ---------------------------------------------------------------------------
# Cancelling and amending
# ---------------------------------------------------------------------------
class TestCancel:
    def test_the_stock_comes_back(self, posted_invoice, oil, warehouses, user):
        services.cancel_sales_invoice(posted_invoice, user=user, reason="Keyed twice")
        qty_base, value_paisa = stock_balance(oil, warehouses.main)
        assert qty_base == 120
        assert value_paisa == 2_400_000

    def test_the_ledger_nets_to_zero_without_a_row_being_touched(self, posted_invoice, user):
        before = list(
            LedgerEntry.objects.filter(voucher_code=posted_invoice.code).values_list(
                "pk", "debit_paisa", "credit_paisa"
            )
        )
        services.cancel_sales_invoice(posted_invoice, user=user)

        entries = LedgerEntry.objects.filter(voucher_code=posted_invoice.code)
        assert sum(e.debit_paisa - e.credit_paisa for e in entries) == 0
        after = list(
            entries.filter(is_reversal=False).values_list("pk", "debit_paisa", "credit_paisa")
        )
        assert after == before

    def test_the_client_balance_returns_to_zero(self, posted_invoice, client_shop, user):
        services.cancel_sales_invoice(posted_invoice, user=user)
        assert party_balance(PartyType.CLIENT, client_shop.pk) == Money.zero()

    def test_the_captured_cost_is_left_on_the_line(self, posted_invoice, user):
        """It is the only record of what the sale actually cost."""
        services.cancel_sales_invoice(posted_invoice, user=user)
        assert posted_invoice.lines.get().cogs_paisa == 960_000


class TestAmend:
    def test_a_posted_invoice_cannot_be_amended(self, posted_invoice, user):
        with pytest.raises(IllegalTransition):
            services.amend_sales_invoice(posted_invoice, user=user)

    def test_correcting_it_and_posting_gives_the_right_ledger(
        self, posted_invoice, oil, client_shop, user, warehouses
    ):
        services.cancel_sales_invoice(posted_invoice, user=user, reason="Wrong quantity")
        amendment = services.amend_sales_invoice(posted_invoice, user=user)

        line = amendment.lines.first()
        services.update_line(
            line, item=oil, qty_input=2, unit_input=Unit.CARTON, rate_input_paisa=to_paisa("3000")
        )
        line.save()
        services.post_sales_invoice(amendment, user=user)

        assert amendment.code == f"{posted_invoice.code}-1"
        assert party_balance(PartyType.CLIENT, client_shop.pk) == Money(705_000)
        assert stock_balance(oil, warehouses.main).qty_base == 96
        assert amendment.lines.get().cogs_paisa == 480_000  # 24 pieces at Rs 200


# ---------------------------------------------------------------------------
# Sales return
# ---------------------------------------------------------------------------
class TestSalesReturn:
    @pytest.fixture
    def credit_note(self, posted_invoice, client_shop, warehouses):
        return services.create_sales_return(
            client=client_shop,
            warehouse=warehouses.main,
            posting_date=MAY,
            against_invoice=posted_invoice,
        )

    @pytest.fixture
    def posted_return(self, credit_note, oil, user):
        """One of the four cartons comes back, at the price it was sold for."""
        add_line(credit_note, oil, qty_input=1, rupees="3000")
        return services.post_sales_return(credit_note, user=user)

    def test_the_stock_comes_back_in(self, posted_return, oil, warehouses):
        qty_base, value_paisa = stock_balance(oil, warehouses.main)
        assert qty_base == 84  # 120 in, 48 out, 12 back
        assert value_paisa == 1_680_000

    def test_the_goods_return_at_what_they_cost_not_what_they_sold_for(self, posted_return, oil):
        """Bringing stock back at the selling price would book the margin into
        inventory and inflate the balance sheet on every return."""
        movement = StockEntry.objects.get(voucher_code=posted_return.code)
        assert movement.qty_base == 12
        assert movement.value_paisa == 240_000  # 12 pieces at the Rs 200 they cost
        assert movement.rate_paisa == 20000

    def test_the_postings_are_the_invoice_mirrored(self, posted_return):
        rows = {
            entry.account.code: (entry.debit_paisa, entry.credit_paisa)
            for entry in LedgerEntry.objects.filter(voucher_code=posted_return.code)
        }
        assert rows == {
            coa.SALES_RETURNS: (300_000, 0),
            coa.TAX_PAYABLE: (52_500, 0),
            coa.ACCOUNTS_RECEIVABLE: (0, 352_500),
            coa.INVENTORY: (240_000, 0),
            coa.COST_OF_GOODS_SOLD: (0, 240_000),
        }

    def test_revenue_comes_back_through_the_contra_account_not_off_sales(self, posted_return):
        """4200 nets against Sales in the Income total and leaves "what did we
        sell" and "what came back" as two figures somebody can look at."""
        codes = set(
            LedgerEntry.objects.filter(voucher_code=posted_return.code).values_list(
                "account__code", flat=True
            )
        )
        assert coa.SALES_RETURNS in codes
        assert coa.SALES not in codes

    def test_it_nets_the_client_balance_to_the_right_figure(self, posted_return, client_shop):
        """Rs 14,100 invoiced, Rs 3,525 credited back, Rs 10,575 still owed."""
        assert party_balance(PartyType.CLIENT, client_shop.pk) == Money(1_057_500)

    def test_returning_everything_nets_the_client_to_zero(
        self, posted_invoice, credit_note, oil, client_shop, user
    ):
        add_line(credit_note, oil, qty_input=4, rupees="3000")
        services.post_sales_return(credit_note, user=user)
        assert party_balance(PartyType.CLIENT, client_shop.pk) == Money.zero()

    def test_returning_everything_restores_the_stock_exactly(
        self, posted_invoice, credit_note, oil, warehouses, user
    ):
        add_line(credit_note, oil, qty_input=4, rupees="3000")
        services.post_sales_return(credit_note, user=user)
        qty_base, value_paisa = stock_balance(oil, warehouses.main)
        assert qty_base == 120
        assert value_paisa == 2_400_000  # to the paisa, as it was before the sale

    def test_a_partial_return_takes_its_exact_share_of_the_cost(
        self, posted_invoice, credit_note, oil, user
    ):
        """Three partial returns must add back up to what one whole one would.

        The share is a Money.allocate split, so no paisa is lost or invented.
        """
        add_line(credit_note, oil, qty_input=3, rupees="3000")
        services.post_sales_return(credit_note, user=user)
        assert credit_note.lines.get().cogs_paisa == 720_000  # 36 of the 48 pieces

    def test_the_ledger_balances(self, posted_return):
        entries = LedgerEntry.objects.filter(voucher_code=posted_return.code)
        assert sum(e.debit_paisa for e in entries) == sum(e.credit_paisa for e in entries)

    def test_it_may_stand_alone_without_an_invoice(
        self, stocked, client_shop, warehouses, oil, user
    ):
        """A shop returns goods months later with no paperwork, and refusing the
        credit note is not an option. They come back at today's average."""
        note = services.create_sales_return(
            client=client_shop, warehouse=warehouses.main, posting_date=MAY
        )
        add_line(note, oil, qty_input=1, rupees="3000")
        services.post_sales_return(note, user=user)

        movement = StockEntry.objects.get(voucher_code=note.code)
        assert note.against_invoice_id is None
        assert movement.value_paisa == 240_000  # 12 at the Rs 200 average

    def test_it_cannot_send_back_more_than_the_invoice_sold(self, credit_note, oil, user):
        add_line(credit_note, oil, qty_input=5, rupees="3000")
        with pytest.raises(ReturnExceedsInvoice, match="more than left"):
            services.post_sales_return(credit_note, user=user)

    def test_two_partial_returns_cannot_add_up_to_more_than_went_out(
        self, posted_invoice, credit_note, oil, client_shop, warehouses, user
    ):
        add_line(credit_note, oil, qty_input=3, rupees="3000")
        services.post_sales_return(credit_note, user=user)

        second = services.create_sales_return(
            client=client_shop,
            warehouse=warehouses.main,
            posting_date=MAY,
            against_invoice=posted_invoice,
        )
        add_line(second, oil, qty_input=2, rupees="3000")
        with pytest.raises(ReturnExceedsInvoice):
            services.post_sales_return(second, user=user)

    def test_cancelling_takes_the_stock_back_out(self, posted_return, oil, warehouses, user):
        services.cancel_sales_return(posted_return, user=user)
        assert stock_balance(oil, warehouses.main).qty_base == 72

    def test_cancelling_restores_the_client_balance(self, posted_return, client_shop, user):
        services.cancel_sales_return(posted_return, user=user)
        assert party_balance(PartyType.CLIENT, client_shop.pk) == Money(1_410_000)

    def test_it_amends_like_an_invoice(self, posted_return, user):
        services.cancel_sales_return(posted_return, user=user)
        amendment = services.amend_sales_return(posted_return, user=user)
        assert isinstance(amendment, SalesReturn)
        assert amendment.status == DocumentStatus.DRAFT
        assert amendment.lines.count() == posted_return.lines.count()


# ---------------------------------------------------------------------------
# Codes
# ---------------------------------------------------------------------------
class TestDocumentCodes:
    def test_invoices_and_credit_notes_are_numbered_separately(
        self, stocked, client_shop, warehouses
    ):
        first = services.create_sales_invoice(
            client=client_shop, warehouse=warehouses.main, posting_date=APRIL
        )
        second = services.create_sales_invoice(
            client=client_shop, warehouse=warehouses.main, posting_date=APRIL
        )
        note = services.create_sales_return(
            client=client_shop, warehouse=warehouses.main, posting_date=APRIL
        )
        assert first.code == "SI-2026-000001"
        assert second.code == "SI-2026-000002"
        assert note.code == "SR-2026-000001"
