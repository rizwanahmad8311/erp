"""Posting, cancelling and amending a purchase invoice and a purchase return.

The rounding rule has its own file — tests/test_purchase_rounding.py. This one
is about what lands in the two ledgers, and about the three things a document
must refuse: posting twice, cancelling something that has been paid, and
amending something that has not been cancelled.

Every assertion about a balance here reads it back out of the ledger through
``apps.accounting.services`` rather than off a header field. That is not
pedantry — a test that asserted ``invoice.total_paisa`` would pass just as
happily if nothing had been posted at all.
"""

import datetime as dt

import pytest

from apps.accounting import chart as coa
from apps.accounting.enums import PartyType
from apps.accounting.exceptions import AlreadyPosted, AlreadyReversed
from apps.accounting.models import LedgerEntry, StockEntry
from apps.accounting.services import account_balance, party_balance, stock_balance
from apps.core import lifecycle
from apps.core.enums import DocumentStatus
from apps.core.exceptions import DocumentImmutable, IllegalTransition
from apps.core.money import Money, to_paisa
from apps.masters.enums import Unit
from apps.masters.models import Item, Vendor
from apps.purchasing import services
from apps.purchasing.exceptions import EmptyDocument, PaymentAllocated, PurchasingError
from apps.purchasing.models import (
    PurchaseInvoice,
    PurchaseInvoiceLine,
    PurchaseReturn,
    PurchaseReturnLine,
)

pytestmark = pytest.mark.django_db

APRIL = dt.date(2026, 4, 1)
MAY = dt.date(2026, 5, 1)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def vendor(db):
    return Vendor.objects.create(code="V-01", name="Unilever Pakistan Ltd")


@pytest.fixture
def oil(db):
    """Twelve to a carton, 17.5% tax. Rs 2,400 a carton divides exactly."""
    return Item.objects.create(
        code="OIL-1000", name="Cooking Oil 1L", carton_size=12, tax_rate_bp=1750
    )


@pytest.fixture
def tea(db):
    """Twenty-four to a carton — the packing where rates do not divide."""
    return Item.objects.create(code="TEA-190", name="Tea 190g", carton_size=24, tax_rate_bp=1750)


@pytest.fixture
def invoice(db, accounts, warehouses, vendor):
    """A DRAFT invoice with a real allocated code and no lines yet."""
    return services.create_purchase_invoice(
        vendor=vendor,
        warehouse=warehouses.main,
        posting_date=APRIL,
        vendor_bill_no="INV-88123",
        vendor_bill_date=APRIL,
    )


def add_line(document, item, *, qty_input, unit_input=Unit.CARTON, rupees, discount="0", **kwargs):
    """Append a line the way the service layer does: amounts from compute_line."""
    model = PurchaseInvoiceLine if isinstance(document, PurchaseInvoice) else PurchaseReturnLine
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
    """10 cartons of 12 at Rs 2,400 — the worked example, posted.

    120 pieces, Rs 24,000 of goods, Rs 4,200 of input tax, Rs 28,200 payable.
    """
    add_line(invoice, oil, qty_input=10, rupees="2400")
    return services.post_purchase_invoice(invoice, user=user)


# ---------------------------------------------------------------------------
# Posting an invoice
# ---------------------------------------------------------------------------
class TestPostPurchaseInvoice:
    def test_the_header_totals_are_the_exact_sum_of_the_lines(self, posted_invoice):
        assert posted_invoice.subtotal_paisa == 2_400_000
        assert posted_invoice.discount_paisa == 0
        assert posted_invoice.tax_paisa == 420_000  # 17.5% of 24,000
        assert posted_invoice.total_paisa == 2_820_000

    def test_it_becomes_posted_and_stamped(self, posted_invoice, user):
        assert posted_invoice.status == DocumentStatus.POSTED
        assert posted_invoice.posted_at is not None
        assert posted_invoice.posted_by == user

    def test_the_stock_arrives_in_the_named_warehouse(self, posted_invoice, oil, warehouses):
        qty_base, value_paisa = stock_balance(oil, warehouses.main)
        assert qty_base == 120
        assert value_paisa == 2_400_000

    def test_the_stock_is_valued_at_the_bill_not_at_the_rounded_rate(
        self, invoice, tea, warehouses, user
    ):
        """The whole point. 10 cartons of 24 at Rs 2,500 is Rs 25,000 exactly.

        240 x the rounded per-piece rate would be Rs 25,000.80. Inventory holds
        the bill.
        """
        add_line(invoice, tea, qty_input=10, rupees="2500")
        services.post_purchase_invoice(invoice, user=user)

        (entry,) = StockEntry.objects.filter(item=tea)
        assert entry.qty_base == 240
        assert entry.value_paisa == 2_500_000  # the bill
        assert entry.rate_paisa == 10417  # the card's rounded average
        assert entry.qty_base * entry.rate_paisa == 2_500_080  # never posted anywhere

    def test_the_general_ledger_is_the_four_expected_rows(self, posted_invoice, accounts):
        rows = {
            entry.account.code: (entry.debit_paisa, entry.credit_paisa)
            for entry in LedgerEntry.objects.filter(voucher_code=posted_invoice.code)
        }
        assert rows == {
            coa.INVENTORY: (2_400_000, 0),
            coa.TAX_PAYABLE: (420_000, 0),
            coa.ACCOUNTS_PAYABLE: (0, 2_820_000),
        }

    def test_the_ledger_balances(self, posted_invoice):
        entries = LedgerEntry.objects.filter(voucher_code=posted_invoice.code)
        assert sum(e.debit_paisa for e in entries) == sum(e.credit_paisa for e in entries)

    def test_inventory_in_the_ledger_equals_the_stock_ledger_exactly(
        self, invoice, tea, warehouses, user
    ):
        """The cross-ledger check the rounding design exists to pass.

        Run against the packing where nothing divides, because that is where a
        naive implementation puts the two ledgers 80 paisa apart.
        """
        add_line(invoice, tea, qty_input=10, rupees="2500")
        add_line(invoice, tea, qty_input=7, rupees="999.99")
        services.post_purchase_invoice(invoice, user=user)

        inventory = account_balance(
            LedgerEntry.objects.get(voucher_code=invoice.code, account__code=coa.INVENTORY).account
        )
        _, stock_value = stock_balance(tea, warehouses.main)
        assert inventory == Money(stock_value)

    def test_the_payable_is_tagged_with_the_vendor(self, posted_invoice, vendor):
        """Which is what makes the supplier's balance aggregable at all."""
        assert party_balance(PartyType.VENDOR, vendor.pk) == Money(2_820_000)

    def test_a_discount_is_credited_to_discount_received(
        self, invoice, oil, accounts, user, vendor
    ):
        add_line(invoice, oil, qty_input=10, rupees="2400", discount="400")
        services.post_purchase_invoice(invoice, user=user)

        rows = {
            entry.account.code: (entry.debit_paisa, entry.credit_paisa)
            for entry in LedgerEntry.objects.filter(voucher_code=invoice.code)
        }
        # Inventory at the gross, discount taken as income, payable at the net + tax.
        assert rows[coa.INVENTORY] == (2_400_000, 0)
        assert rows[coa.DISCOUNT_RECEIVED] == (0, 40_000)
        assert rows[coa.TAX_PAYABLE] == (413_000, 0)  # 17.5% of 2,360,000
        assert rows[coa.ACCOUNTS_PAYABLE] == (0, 2_773_000)
        assert sum(d for d, _ in rows.values()) == sum(c for _, c in rows.values())

    def test_several_lines_post_one_stock_row_each(self, invoice, oil, tea, user, warehouses):
        add_line(invoice, oil, qty_input=10, rupees="2400")
        add_line(invoice, tea, qty_input=5, rupees="2500")
        services.post_purchase_invoice(invoice, user=user)

        assert StockEntry.objects.filter(voucher_code=invoice.code).count() == 2
        assert stock_balance(oil, warehouses.main).qty_base == 120
        assert stock_balance(tea, warehouses.main).qty_base == 120

    def test_a_free_line_carries_stock_in_at_no_cost(self, invoice, oil, user, warehouses):
        """Bonus cartons are real, and they genuinely drag the average down."""
        add_line(invoice, oil, qty_input=10, rupees="2400")
        add_line(invoice, oil, qty_input=1, rupees="0")
        services.post_purchase_invoice(invoice, user=user)

        qty_base, value_paisa = stock_balance(oil, warehouses.main)
        assert qty_base == 132
        assert value_paisa == 2_400_000


class TestPostingIsRefused:
    def test_when_the_document_has_no_lines(self, invoice, user):
        with pytest.raises(EmptyDocument):
            services.post_purchase_invoice(invoice, user=user)

    def test_when_it_has_already_been_posted(self, posted_invoice, user):
        with pytest.raises(IllegalTransition):
            services.post_purchase_invoice(posted_invoice, user=user)

    def test_forcing_the_status_back_to_draft_does_not_get_past_immutability(
        self, posted_invoice, user
    ):
        """Belt and braces. Faking the status in memory to slip past
        ``assert_transition`` runs straight into ``DocumentModel.save``, which
        compares against what was loaded from the database rather than trusting
        the instance. Nothing is written."""
        posted_invoice.status = DocumentStatus.DRAFT  # in memory only
        with pytest.raises(DocumentImmutable):
            services.post_purchase_invoice(posted_invoice, user=user)

        assert LedgerEntry.objects.filter(voucher_code=posted_invoice.code).count() == 3
        assert StockEntry.objects.filter(voucher_code=posted_invoice.code).count() == 1

    def test_the_accounting_layer_refuses_a_double_post_of_its_own_accord(
        self, posted_invoice, user
    ):
        """The guard under the guard: even handed the same voucher directly, the
        ledger refuses to write a second set of rows for it."""
        from apps.accounting.services import post_entries

        with pytest.raises(AlreadyPosted):
            post_entries(
                posted_invoice,
                [gl.as_entry() for gl in services.build_invoice_gl(posted_invoice)],
                posted_invoice.posting_date,
            )

    def test_a_posted_document_writes_nothing_when_it_fails(self, invoice, oil, user, warehouses):
        """A failure part-way leaves both ledgers exactly as they were."""
        add_line(invoice, oil, qty_input=10, rupees="2400")
        invoice.warehouse = None  # will blow up inside the posting
        with pytest.raises(Exception):  # noqa: B017 - any failure must roll back
            services.post_purchase_invoice(invoice, user=user)

        assert not LedgerEntry.objects.filter(voucher_code=invoice.code).exists()
        assert not StockEntry.objects.filter(voucher_code=invoice.code).exists()


class TestAPostedDocumentIsFrozen:
    def test_the_header_cannot_be_edited(self, posted_invoice):
        posted_invoice.remarks = "changed my mind"
        with pytest.raises(DocumentImmutable):
            posted_invoice.save()

    def test_its_lines_cannot_be_edited(self, posted_invoice):
        """DocumentModel guards its own row; the line guards itself."""
        line = posted_invoice.lines.first()
        line.qty_input = 99
        with pytest.raises(PurchasingError, match="cannot be modified"):
            line.save()

    def test_its_lines_cannot_be_deleted(self, posted_invoice):
        line = posted_invoice.lines.first()
        with pytest.raises(PurchasingError, match="cannot be deleted"):
            line.delete()

    def test_it_cannot_be_deleted(self, posted_invoice):
        with pytest.raises(DocumentImmutable):
            posted_invoice.delete()

    def test_a_draft_may_be_deleted(self, invoice, oil):
        """It has written nothing to any ledger, so there is nothing to lose."""
        add_line(invoice, oil, qty_input=1, rupees="2400")
        invoice.delete()
        assert not PurchaseInvoice.objects.filter(pk=invoice.pk).exists()


# ---------------------------------------------------------------------------
# Cancelling
# ---------------------------------------------------------------------------
class TestCancelPurchaseInvoice:
    def test_the_stock_goes_back_out(self, posted_invoice, oil, warehouses, user):
        services.cancel_purchase_invoice(posted_invoice, user=user, reason="Wrong supplier")
        qty_base, value_paisa = stock_balance(oil, warehouses.main)
        assert qty_base == 0
        assert value_paisa == 0

    def test_the_ledger_nets_to_zero_without_a_row_being_touched(self, posted_invoice, user):
        before = list(
            LedgerEntry.objects.filter(voucher_code=posted_invoice.code).values_list(
                "pk", "debit_paisa", "credit_paisa"
            )
        )
        services.cancel_purchase_invoice(posted_invoice, user=user)

        entries = LedgerEntry.objects.filter(voucher_code=posted_invoice.code)
        assert sum(e.debit_paisa - e.credit_paisa for e in entries) == 0
        assert entries.filter(is_reversal=True).count() == len(before)
        # The originals are untouched: same rows, same amounts.
        after = list(
            entries.filter(is_reversal=False).values_list("pk", "debit_paisa", "credit_paisa")
        )
        assert after == before

    def test_the_reversal_is_new_rows_not_deleted_ones(self, posted_invoice, user):
        services.cancel_purchase_invoice(posted_invoice, user=user)
        assert LedgerEntry.objects.filter(voucher_code=posted_invoice.code).count() == 6
        assert StockEntry.objects.filter(voucher_code=posted_invoice.code).count() == 2

    def test_the_vendor_balance_returns_to_zero(self, posted_invoice, vendor, user):
        services.cancel_purchase_invoice(posted_invoice, user=user)
        assert party_balance(PartyType.VENDOR, vendor.pk) == Money.zero()

    def test_it_is_stamped_with_who_and_why(self, posted_invoice, user):
        services.cancel_purchase_invoice(posted_invoice, user=user, reason="Duplicate entry")
        assert posted_invoice.status == DocumentStatus.CANCELLED
        assert posted_invoice.cancelled_by == user
        assert posted_invoice.cancel_reason == "Duplicate entry"

    def test_cancelling_twice_is_refused(self, posted_invoice, user):
        services.cancel_purchase_invoice(posted_invoice, user=user)
        with pytest.raises(IllegalTransition):
            services.cancel_purchase_invoice(posted_invoice, user=user)

    def test_a_draft_cannot_be_cancelled(self, invoice, oil, user):
        """There is nothing to reverse. Delete it instead."""
        add_line(invoice, oil, qty_input=1, rupees="2400")
        with pytest.raises(IllegalTransition):
            services.cancel_purchase_invoice(invoice, user=user)

    def test_cancelling_is_allowed_even_when_the_stock_has_been_sold_on(
        self, posted_invoice, oil, warehouses, user, monkeypatch
    ):
        """A document must always be cancellable, even into negative stock.

        Refusing here would trap the invoice in POSTED with no legal move left.
        """
        from django.test import override_settings

        with override_settings(ALLOW_NEGATIVE_STOCK=True):
            from apps.accounting.services import post_stock
            from tests.testapp.models import SampleDocument

            other = SampleDocument.objects.create(code="SI-2026-000900", party_name="Shop")
            post_stock(
                other,
                [{"item": oil, "warehouse": warehouses.main, "qty_base": -120}],
                APRIL,
            )
            services.cancel_purchase_invoice(posted_invoice, user=user)

        assert posted_invoice.status == DocumentStatus.CANCELLED
        assert stock_balance(oil, warehouses.main).qty_base == -120


class TestCancelIsBlockedWhenPaid:
    """The one refusal that is about another document entirely.

    A payment is its own voucher with its own ledger rows. Reversing this
    invoice would leave that payment sitting against a supplier balance with no
    invoice under it — money paid against nothing.
    """

    @pytest.fixture
    def paid(self, monkeypatch):
        """Two payments against this invoice, without going through payments.

        ``apps.core.lifecycle.payment_allocations`` is the documented seam: it
        asks ``apps.payments.services.allocations_for`` and hands back what it
        finds. Patching the seam itself exercises the guard exactly as a real
        allocation does, and keeps this file about purchasing.
        """

        def two_payments(document):
            return [
                lifecycle.Allocation(code="PV-2026-000012", amount_paisa=1_500_000),
                lifecycle.Allocation(code="PV-2026-000031", amount_paisa=800_000),
            ]

        monkeypatch.setattr(lifecycle, "payment_allocations", two_payments)

    def test_it_refuses(self, posted_invoice, paid, user):
        with pytest.raises(PaymentAllocated):
            services.cancel_purchase_invoice(posted_invoice, user=user)

    def test_the_message_names_the_payments(self, posted_invoice, paid, user):
        with pytest.raises(PaymentAllocated) as caught:
            services.cancel_purchase_invoice(posted_invoice, user=user)

        message = str(caught.value)
        assert "PV-2026-000012" in message
        assert "PV-2026-000031" in message
        # Each payment with its own figure, so the refusal can be checked
        # against the payments themselves rather than against a sum of them.
        assert "15,000.00" in message
        assert "8,000.00" in message

    def test_the_payments_are_available_to_a_view_without_re_deriving_them(
        self, posted_invoice, paid, user
    ):
        with pytest.raises(PaymentAllocated) as caught:
            services.cancel_purchase_invoice(posted_invoice, user=user)
        assert [a.code for a in caught.value.payments] == ["PV-2026-000012", "PV-2026-000031"]

    def test_nothing_is_reversed(self, posted_invoice, paid, user, oil, warehouses):
        with pytest.raises(PaymentAllocated):
            services.cancel_purchase_invoice(posted_invoice, user=user)

        assert posted_invoice.status == DocumentStatus.POSTED
        assert not LedgerEntry.objects.filter(
            voucher_code=posted_invoice.code, is_reversal=True
        ).exists()
        assert stock_balance(oil, warehouses.main).qty_base == 120

    def test_an_unpaid_invoice_cancels_normally(self, posted_invoice, user):
        """The same guard, proving it is not simply always refusing."""
        assert services.payment_allocations(posted_invoice) == []
        services.cancel_purchase_invoice(posted_invoice, user=user)
        assert posted_invoice.status == DocumentStatus.CANCELLED


class TestPaidPaisaIsDerived:
    def test_there_is_no_paid_column(self):
        """CLAUDE.md §6: a running balance on a header is a number that can lie."""
        columns = {field.name for field in PurchaseInvoice._meta.get_fields()}
        assert "paid_paisa" not in columns

    def test_it_reads_zero_while_payments_does_not_exist(self, posted_invoice):
        assert posted_invoice.paid_paisa == 0
        assert posted_invoice.outstanding_paisa == posted_invoice.total_paisa
        assert posted_invoice.is_paid is False

    def test_it_sums_the_allocations_when_there_are_some(self, posted_invoice, monkeypatch):
        monkeypatch.setattr(
            lifecycle,
            "payment_allocations",
            lambda document: [lifecycle.Allocation("PV-1", 2_820_000)],
        )
        assert posted_invoice.paid_paisa == 2_820_000
        assert posted_invoice.outstanding_paisa == 0
        assert posted_invoice.is_paid is True


# ---------------------------------------------------------------------------
# Amending
# ---------------------------------------------------------------------------
class TestAmend:
    def test_a_posted_invoice_cannot_be_amended(self, posted_invoice, user):
        """Nothing has been reversed yet, so amending would double-count it."""
        with pytest.raises(IllegalTransition):
            services.amend_purchase_invoice(posted_invoice, user=user)

    @pytest.fixture
    def amendment(self, posted_invoice, user):
        services.cancel_purchase_invoice(posted_invoice, user=user, reason="Wrong quantity")
        return services.amend_purchase_invoice(posted_invoice, user=user)

    def test_it_is_a_new_draft_that_points_back(self, amendment, posted_invoice):
        assert amendment.status == DocumentStatus.DRAFT
        assert amendment.amended_from == posted_invoice
        assert amendment.amendment_no == 1

    def test_its_code_suffixes_the_original(self, amendment, posted_invoice):
        assert amendment.code == f"{posted_invoice.code}-1"

    def test_the_lines_come_across_intact(self, amendment, posted_invoice):
        original = posted_invoice.lines.first()
        copied = amendment.lines.first()
        assert amendment.lines.count() == posted_invoice.lines.count()
        assert (copied.item_id, copied.qty_input, copied.unit_input) == (
            original.item_id,
            original.qty_input,
            original.unit_input,
        )
        assert copied.amount_paisa == original.amount_paisa

    def test_the_header_matches_the_copied_lines(self, amendment, posted_invoice):
        assert amendment.total_paisa == posted_invoice.total_paisa

    def test_correcting_it_and_posting_gives_the_right_ledger(
        self, amendment, oil, vendor, user, warehouses
    ):
        """The whole point of an amendment: the corrected figures, once."""
        line = amendment.lines.first()
        services.update_line(
            line,
            item=oil,
            qty_input=6,
            unit_input=Unit.CARTON,
            rate_input_paisa=to_paisa("2400"),
        )
        line.save()
        services.post_purchase_invoice(amendment, user=user)

        # The original was cancelled and nets to zero, so the vendor owes only
        # what the amendment says.
        assert party_balance(PartyType.VENDOR, vendor.pk) == Money(1_692_000)
        assert stock_balance(oil, warehouses.main).qty_base == 72

    def test_amending_twice_suffixes_the_root_not_the_amendment(self, amendment, user, oil):
        """SI-000123 -> -1 -> -2, never -1-1."""
        root_code = amendment.amended_from.code
        services.post_purchase_invoice(amendment, user=user)
        services.cancel_purchase_invoice(amendment, user=user)
        second = services.amend_purchase_invoice(amendment, user=user)
        assert second.code == f"{root_code}-2"

    def test_the_supplier_bill_number_may_be_reused_by_the_amendment(self, amendment):
        """A cancelled document releases the bill number it was holding.

        Otherwise the unique constraint on (vendor, bill no) would make a typo
        in a bill impossible to correct.
        """
        assert amendment.vendor_bill_no == "INV-88123"


# ---------------------------------------------------------------------------
# Purchase return
# ---------------------------------------------------------------------------
class TestPurchaseReturn:
    @pytest.fixture
    def credit_note(self, posted_invoice, vendor, warehouses):
        return services.create_purchase_return(
            vendor=vendor,
            warehouse=warehouses.main,
            posting_date=MAY,
            vendor_bill_no="CN-4401",
        )

    @pytest.fixture
    def posted_return(self, credit_note, oil, user):
        """Four of the ten cartons go back, at the price they came in at."""
        add_line(credit_note, oil, qty_input=4, rupees="2400")
        return services.post_purchase_return(credit_note, user=user)

    def test_the_stock_goes_back_out(self, posted_return, oil, warehouses):
        qty_base, value_paisa = stock_balance(oil, warehouses.main)
        assert qty_base == 72  # 120 in, 48 back out
        assert value_paisa == 1_440_000

    def test_the_postings_are_the_invoice_mirrored(self, posted_return):
        rows = {
            entry.account.code: (entry.debit_paisa, entry.credit_paisa)
            for entry in LedgerEntry.objects.filter(voucher_code=posted_return.code)
        }
        assert rows == {
            coa.ACCOUNTS_PAYABLE: (1_128_000, 0),  # Rs 9,600 + 17.5% tax
            coa.INVENTORY: (0, 960_000),
            coa.TAX_PAYABLE: (0, 168_000),
        }

    def test_the_ledger_balances(self, posted_return):
        entries = LedgerEntry.objects.filter(voucher_code=posted_return.code)
        assert sum(e.debit_paisa for e in entries) == sum(e.credit_paisa for e in entries)

    def test_it_reduces_what_is_owed_to_the_vendor(self, posted_return, vendor):
        # Rs 28,200 billed, Rs 11,280 credited back.
        assert party_balance(PartyType.VENDOR, vendor.pk) == Money(1_692_000)

    def test_the_goods_leave_at_cost_not_at_the_credit_note_rate(
        self, posted_invoice, credit_note, oil, warehouses, user
    ):
        """The one line that is not a mirror, and cannot be.

        A second receipt at a different price moves the average. The credit note
        still says Rs 2,400 a carton, but the goods are now carried at less than
        that, and the difference is a real gain that has to be visible.
        """
        cheaper = services.create_purchase_invoice(
            vendor=posted_invoice.vendor, warehouse=warehouses.main, posting_date=APRIL
        )
        add_line(cheaper, oil, qty_input=10, rupees="1200")
        services.post_purchase_invoice(cheaper, user=user)
        # 240 pieces holding Rs 36,000 — an average of Rs 150 a piece.

        add_line(credit_note, oil, qty_input=4, rupees="2400")
        services.post_purchase_return(credit_note, user=user)

        rows = {
            entry.account.code: (entry.debit_paisa, entry.credit_paisa)
            for entry in LedgerEntry.objects.filter(voucher_code=credit_note.code)
        }
        assert rows[coa.INVENTORY] == (0, 720_000)  # 48 pieces at Rs 150
        assert rows[coa.OTHER_INCOME] == (0, 240_000)  # credited 9,600, cost 7,200
        assert sum(d for d, _ in rows.values()) == sum(c for _, c in rows.values())

    def test_a_loss_lands_in_miscellaneous_expenses(
        self, posted_invoice, credit_note, oil, warehouses, user
    ):
        """The other direction: credited less than the goods are carried at."""
        dearer = services.create_purchase_invoice(
            vendor=posted_invoice.vendor, warehouse=warehouses.main, posting_date=APRIL
        )
        add_line(dearer, oil, qty_input=10, rupees="4800")
        services.post_purchase_invoice(dearer, user=user)
        # 240 pieces holding Rs 72,000 — an average of Rs 300 a piece.

        add_line(credit_note, oil, qty_input=4, rupees="2400")
        services.post_purchase_return(credit_note, user=user)

        rows = {
            entry.account.code: (entry.debit_paisa, entry.credit_paisa)
            for entry in LedgerEntry.objects.filter(voucher_code=credit_note.code)
        }
        assert rows[coa.INVENTORY] == (0, 1_440_000)  # 48 at Rs 300
        assert rows[coa.MISCELLANEOUS_EXPENSES] == (480_000, 0)
        assert sum(d for d, _ in rows.values()) == sum(c for _, c in rows.values())

    def test_the_preview_agrees_with_what_gets_posted(self, credit_note, oil, user):
        """The entry screen's estimate and the posting must not diverge."""
        add_line(credit_note, oil, qty_input=4, rupees="2400")
        predicted = services.preview_return_cost_paisa(credit_note)
        services.post_purchase_return(credit_note, user=user)

        actual = -sum(
            entry.value_paisa for entry in StockEntry.objects.filter(voucher_code=credit_note.code)
        )
        assert predicted == actual

    def test_cancelling_puts_the_stock_back(self, posted_return, oil, warehouses, user):
        services.cancel_purchase_return(posted_return, user=user)
        assert stock_balance(oil, warehouses.main).qty_base == 120

    def test_cancelling_nets_its_ledger_to_zero(self, posted_return, vendor, user):
        services.cancel_purchase_return(posted_return, user=user)
        entries = LedgerEntry.objects.filter(voucher_code=posted_return.code)
        assert sum(e.debit_paisa - e.credit_paisa for e in entries) == 0
        assert party_balance(PartyType.VENDOR, vendor.pk) == Money(2_820_000)

    def test_it_cannot_be_reversed_twice(self, posted_return, user):
        services.cancel_purchase_return(posted_return, user=user)
        posted_return.status = DocumentStatus.POSTED  # in memory only
        with pytest.raises(AlreadyReversed):
            services.cancel_purchase_return(posted_return, user=user)

    def test_it_amends_like_an_invoice(self, posted_return, user):
        services.cancel_purchase_return(posted_return, user=user)
        amendment = services.amend_purchase_return(posted_return, user=user)
        assert isinstance(amendment, PurchaseReturn)
        assert amendment.status == DocumentStatus.DRAFT
        assert amendment.lines.count() == posted_return.lines.count()

    def test_returning_more_than_is_held_is_refused(self, credit_note, oil, user):
        """The stock ledger's own guard, reached through a purchase return."""
        from apps.accounting.exceptions import InsufficientStock

        add_line(credit_note, oil, qty_input=99, rupees="2400")
        with pytest.raises(InsufficientStock):
            services.post_purchase_return(credit_note, user=user)


# ---------------------------------------------------------------------------
# Codes
# ---------------------------------------------------------------------------
class TestDocumentCodes:
    def test_invoices_and_returns_are_numbered_separately(self, vendor, warehouses):
        first = services.create_purchase_invoice(
            vendor=vendor, warehouse=warehouses.main, posting_date=APRIL
        )
        second = services.create_purchase_invoice(
            vendor=vendor, warehouse=warehouses.main, posting_date=APRIL
        )
        credit = services.create_purchase_return(
            vendor=vendor, warehouse=warehouses.main, posting_date=APRIL
        )
        assert first.code == "PI-2026-000001"
        assert second.code == "PI-2026-000002"
        assert credit.code == "PR-2026-000001"

    def test_the_year_comes_from_the_posting_date(self, vendor, warehouses):
        document = services.create_purchase_invoice(
            vendor=vendor, warehouse=warehouses.main, posting_date=dt.date(2027, 1, 9)
        )
        assert document.code.startswith("PI-2027-")
