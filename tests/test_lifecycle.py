"""The one contract every document obeys, and the round trip that proves it.

Two halves.

**The audit.** Every concrete :class:`~apps.core.models.DocumentModel` subclass
in the system is discovered and put through the same questions: does it
implement all three lifecycle methods, with the documented signatures; does it
declare its own cancel permission; does its ``cancel`` refuse while something
depends on it; can a report leave it out when it is cancelled. A document type
that answers one of those differently from the others is a screen that behaves
differently from the others, and it is always the one nobody tested.

**The round trip.** For each document type: post it, cancel it, amend the
cancellation into a new draft, post that — and check that the party balance and
the stock balance land *exactly* where a single clean posting would have left
them, that the original entries are still there byte for byte, and that the
whole chain reads back in order. That is the "bill re-check, revert and correct"
requirement, stated as arithmetic.

Every balance here is read from the ledger through ``apps.accounting.services``.
A test that asserted ``invoice.total_paisa`` would pass just as happily if
nothing had been posted at all.
"""

import datetime as dt
import inspect

import pytest
from django.apps import apps as django_apps
from django.contrib.auth.models import Permission

from apps.accounting.enums import PartyType
from apps.accounting.models import LedgerEntry, StockEntry
from apps.accounting.services import (
    party_balance,
    preview_reversal,
    stock_balance,
)
from apps.core.enums import DocumentStatus
from apps.core.exceptions import DocumentHasDependents, IllegalTransition, PaymentAllocated
from apps.core.lifecycle import Dependent
from apps.core.models import DocumentModel
from apps.masters.enums import Unit
from apps.masters.models import Client, Item, Vendor
from apps.payments import services as payments
from apps.payments.enums import PaymentDirection, PaymentMode
from apps.payments.exceptions import ChequeSettled
from apps.payments.models import ChequeEvent, Payment
from apps.purchasing import services as purchasing
from apps.purchasing.models import PurchaseInvoice, PurchaseInvoiceLine, PurchaseReturn
from apps.purchasing.models import PurchaseReturnLine as PRLine
from apps.sales import services as sales
from apps.sales.models import SalesInvoice, SalesInvoiceLine, SalesReturn, SalesReturnLine
from tests.conftest import join_group

pytestmark = pytest.mark.django_db

APRIL = dt.date(2026, 4, 1)
MAY = dt.date(2026, 5, 1)
JUNE = dt.date(2026, 6, 1)

#: 100 cartons of 12 at Rs 2,400 a carton: 1,200 pieces costing Rs 240,000.
STOCK_PIECES = 1200
STOCK_VALUE = 24_000_000
#: Which makes one piece worth exactly Rs 200 — chosen so the moving average is
#: exact and every figure below can be checked by hand.
PIECE_COST = STOCK_VALUE // STOCK_PIECES

#: 10 cartons out at Rs 2,500 a carton: 120 pieces billed at Rs 25,000...
SALE_PIECES = 120
SALE_VALUE = 2_500_000
#: ...which cost Rs 24,000 to buy.
SALE_COST = SALE_PIECES * PIECE_COST

#: 10 cartons back to the supplier, at what we paid for them.
PRETURN_PIECES = 120
PRETURN_VALUE = 2_400_000

REASON = "Quantity on line 1 was wrong"


# ===========================================================================
# Fixtures
# ===========================================================================
@pytest.fixture
def oil(db):
    """Twelve to a carton, no tax — the arithmetic here is about the lifecycle."""
    return Item.objects.create(code="OIL-1000", name="Cooking Oil 1L", carton_size=12)


@pytest.fixture
def vendor(db):
    return Vendor.objects.create(code="V-01", name="Dalda Foods")


@pytest.fixture
def shop(db):
    return Client.objects.create(
        code="C-0001", name="Al-Madina Kiryana", credit_limit_paisa=100_000_000, credit_days=15
    )


def _purchase(vendor, warehouse, oil, *, cartons, rupees, posting_date=APRIL):
    invoice = purchasing.create_purchase_invoice(
        vendor=vendor, warehouse=warehouse, posting_date=posting_date
    )
    line = purchasing.update_line(
        PurchaseInvoiceLine(document=invoice),
        item=oil,
        qty_input=cartons,
        unit_input=Unit.CARTON,
        rate_input_paisa=rupees,
    )
    line.save()
    return invoice


@pytest.fixture
def stocked(db, accounts, warehouses, vendor, oil, user):
    """1,200 pieces on hand at Rs 200 each, bought through a real purchase invoice.

    Posted, not faked: everything downstream values against what this actually
    put into the stock ledger.
    """
    invoice = _purchase(vendor, warehouses.main, oil, cartons=100, rupees=240_000)
    return purchasing.post_purchase_invoice(invoice, user=user)


def _sale(shop, warehouse, oil, *, cartons=10, rupees=250_000, posting_date=MAY, **fields):
    invoice = sales.create_sales_invoice(
        client=shop, warehouse=warehouse, posting_date=posting_date, **fields
    )
    line = sales.update_line(
        SalesInvoiceLine(document=invoice),
        item=oil,
        qty_input=cartons,
        unit_input=Unit.CARTON,
        rate_input_paisa=rupees,
    )
    line.save()
    return invoice


# ===========================================================================
# The audit: every document type answers the same questions
# ===========================================================================
def document_models():
    """Every concrete document in the installed apps, discovered not listed.

    Discovered on purpose. A list would be a list somebody forgets to add the
    seventh document to, and the seventh document is the one that ships without
    a cancel permission.
    """
    return sorted(
        (
            model
            for model in django_apps.get_models()
            if issubclass(model, DocumentModel) and not model._meta.abstract
        ),
        key=lambda model: model._meta.label,
    )


@pytest.mark.parametrize("model", document_models(), ids=lambda m: m._meta.label)
class TestTheContract:
    """CLAUDE.md §5, checked against every document rather than one of them."""

    def test_it_implements_all_three_lifecycle_methods(self, model):
        """The base raises NotImplementedError; a real document must override."""
        for name in ("post", "cancel", "amend"):
            assert getattr(model, name) is not getattr(DocumentModel, name), (
                f"{model._meta.label}.{name}() is still the base's, which only raises."
            )

    def test_the_signatures_match_the_documented_contract(self, model):
        """``post(*, user, **options)``, ``cancel(*, user, reason)``, ``amend(*, user)``.

        Keyword-only with defaults throughout, so a caller holding a document it
        knows nothing about can still post, cancel or amend it — which is
        exactly what the shared cancel screen does.
        """
        post = inspect.signature(model.post).parameters
        assert post["user"].kind is inspect.Parameter.KEYWORD_ONLY
        assert post["user"].default is None
        for extra in post.values():
            if extra.name in {"self", "user"} or extra.kind is inspect.Parameter.VAR_KEYWORD:
                continue
            assert extra.kind is inspect.Parameter.KEYWORD_ONLY, (
                f"{model._meta.label}.post() takes {extra.name} positionally."
            )
            assert extra.default is not inspect.Parameter.empty, (
                f"{model._meta.label}.post() requires {extra.name}, so a generic caller "
                f"cannot post it."
            )

        cancel = inspect.signature(model.cancel).parameters
        assert set(cancel) == {"self", "user", "reason"}
        assert cancel["reason"].default == ""

        amend = inspect.signature(model.amend).parameters
        assert set(amend) == {"self", "user"}

    def test_it_declares_its_own_cancel_permission(self, model):
        """``<app>.cancel_<model>``, or the cancel screen has nothing to check."""
        codename = model.cancel_permission().split(".", 1)[1]
        declared = {name for name, _label in model._meta.permissions}
        assert codename in declared, (
            f"{model._meta.label} has no {codename!r} in Meta.permissions, so "
            f"apps.core.views.cancel_view would refuse everybody."
        )
        assert Permission.objects.filter(
            codename=codename, content_type__app_label=model._meta.app_label
        ).exists(), f"{codename} is declared but has no migration behind it."

    def test_dependents_is_a_read_that_never_raises(self, model):
        """The cancel screen calls it to *show* what blocks, before any refusal."""
        assert model().dependents() == []

    def test_its_default_manager_can_leave_cancelled_rows_out_of_a_report(self, model):
        """``live()`` is what a report reads; the list screens deliberately do not."""
        queryset = model._default_manager.all()
        for name in ("live", "cancelled", "for_report"):
            assert hasattr(queryset, name), f"{model._meta.label}.objects has no {name}()."
        assert not queryset.live().filter(status=DocumentStatus.CANCELLED).exists()

    def test_it_can_say_where_it_lives(self, model):
        """The shared timeline and cancel templates link by ``get_absolute_url``.

        Django's ``Model`` does not define one, so this is a real check that each
        document declared it rather than a check that an attribute exists.
        """
        if model._meta.app_label == "testapp":
            pytest.skip("the pytest-only harness document has no screen")
        assert callable(getattr(model, "get_absolute_url", None)), (
            f"{model._meta.label} has no get_absolute_url(), so nothing can link to it."
        )


# ===========================================================================
# Cancelling refuses while something depends on the document
# ===========================================================================
class TestEveryCancelRunsTheSameGate:
    """Each ``cancel_*`` service must call ``assert_cancellable()`` before it writes.

    Driven by giving each document a blocker it cannot have in real life and
    checking the service refuses. A service that skipped the gate would sail
    past this and reverse the entries.
    """

    @pytest.fixture
    def blocker(self, monkeypatch):
        def block(model):
            monkeypatch.setattr(
                model,
                "dependents",
                lambda self: [
                    Dependent(
                        kind="test blocker",
                        code="XX-2026-000001",
                        detail="stands in the way",
                        action="Remove it first.",
                    )
                ],
            )

        return block

    def _assert_refused(self, service, document, model, blocker):
        blocker(model)
        with pytest.raises(DocumentHasDependents) as caught:
            service(document, user=None, reason=REASON)

        assert document.code in str(caught.value)
        assert "XX-2026-000001" in str(caught.value), "the refusal must name what blocks it"
        assert "Remove it first." in str(caught.value)
        document.refresh_from_db()
        assert document.status == DocumentStatus.POSTED
        assert not LedgerEntry.objects.filter(voucher_code=document.code, is_reversal=True).exists()

    def test_purchase_invoice(self, stocked, blocker):
        self._assert_refused(purchasing.cancel_purchase_invoice, stocked, PurchaseInvoice, blocker)

    def test_purchase_return(self, stocked, vendor, warehouses, oil, user, blocker):
        document = purchasing.create_purchase_return(
            vendor=vendor, warehouse=warehouses.main, posting_date=MAY
        )
        purchasing.update_line(
            PRLine(document=document),
            item=oil,
            qty_input=10,
            unit_input=Unit.CARTON,
            rate_input_paisa=240_000,
        ).save()
        purchasing.post_purchase_return(document, user=user)
        self._assert_refused(purchasing.cancel_purchase_return, document, PurchaseReturn, blocker)

    def test_sales_invoice(self, stocked, shop, warehouses, oil, user, blocker):
        invoice = sales.post_sales_invoice(_sale(shop, warehouses.main, oil), user=user)
        self._assert_refused(sales.cancel_sales_invoice, invoice, SalesInvoice, blocker)

    def test_sales_return(self, stocked, shop, warehouses, oil, user, blocker):
        invoice = sales.post_sales_invoice(_sale(shop, warehouses.main, oil), user=user)
        note = _credit_note(shop, warehouses.main, oil, invoice, cartons=4)
        sales.post_sales_return(note, user=user)
        self._assert_refused(sales.cancel_sales_return, note, SalesReturn, blocker)

    def test_payment(self, shop, accounts, user, blocker):
        payment = payments.post_payment(_receipt(shop, rupees=1_000_000), user=user)
        self._assert_refused(payments.cancel_payment, payment, Payment, blocker)

    def test_cheque_event(self, shop, accounts, user, blocker):
        payment = payments.post_payment(
            _receipt(shop, rupees=1_000_000, mode=PaymentMode.CHEQUE), user=user
        )
        event = payments.clear_cheque(payment, posting_date=JUNE, user=user)
        self._assert_refused(payments.cancel_cheque_event, event, ChequeEvent, blocker)


def _credit_note(shop, warehouse, oil, invoice, *, cartons, rupees=250_000, posting_date=JUNE):
    note = sales.create_sales_return(
        client=shop, warehouse=warehouse, posting_date=posting_date, against_invoice=invoice
    )
    sales.update_line(
        SalesReturnLine(document=note),
        item=oil,
        qty_input=cartons,
        unit_input=Unit.CARTON,
        rate_input_paisa=rupees,
    ).save()
    return note


def _receipt(shop, *, rupees, mode=PaymentMode.CASH, posting_date=MAY):
    extra = {}
    if mode == PaymentMode.CHEQUE:
        extra = {"cheque_no": "0012345", "cheque_date": posting_date, "bank_name": "HBL"}
    return payments.create_payment(
        party=shop,
        direction=PaymentDirection.RECEIVE,
        mode=mode,
        posting_date=posting_date,
        amount_paisa=rupees,
        **extra,
    )


class TestTheRealBlockers:
    """The refusals that exist in the business, not just in the mechanism."""

    def test_an_allocated_invoice_names_the_payment_and_raises_PaymentAllocated(
        self, stocked, shop, warehouses, oil, user
    ):
        invoice = sales.post_sales_invoice(_sale(shop, warehouses.main, oil), user=user)
        payment = payments.post_payment(_receipt(shop, rupees=SALE_VALUE), user=user)
        payments.allocate_payment(payment, [(invoice, SALE_VALUE)], user=user)

        with pytest.raises(PaymentAllocated) as caught:
            sales.cancel_sales_invoice(invoice, user=user, reason=REASON)

        assert payment.code in str(caught.value)
        assert [dependent.code for dependent in caught.value.payments] == [payment.code]

    def test_an_invoice_with_a_credit_note_against_it_names_the_note(
        self, stocked, shop, warehouses, oil, user
    ):
        """The blocker sales had no check for at all before this phase.

        The note took its cost basis from this invoice's lines; reversing the
        invoice underneath it would leave the note crediting the shop against a
        sale the books no longer contain.
        """
        invoice = sales.post_sales_invoice(_sale(shop, warehouses.main, oil), user=user)
        note = sales.post_sales_return(
            _credit_note(shop, warehouses.main, oil, invoice, cartons=4), user=user
        )

        with pytest.raises(DocumentHasDependents) as caught:
            sales.cancel_sales_invoice(invoice, user=user, reason=REASON)

        assert note.code in str(caught.value)
        assert "credit note" in str(caught.value)
        invoice.refresh_from_db()
        assert invoice.status == DocumentStatus.POSTED

    def test_cancelling_the_note_first_frees_the_invoice(
        self, stocked, shop, warehouses, oil, user
    ):
        invoice = sales.post_sales_invoice(_sale(shop, warehouses.main, oil), user=user)
        note = sales.post_sales_return(
            _credit_note(shop, warehouses.main, oil, invoice, cartons=4), user=user
        )

        sales.cancel_sales_return(note, user=user, reason=REASON)
        sales.cancel_sales_invoice(invoice, user=user, reason=REASON)

        assert invoice.status == DocumentStatus.CANCELLED
        assert party_balance(PartyType.CLIENT, shop.pk).paisa == 0
        assert stock_balance(oil, warehouses.main) == (STOCK_PIECES, STOCK_VALUE)

    def test_a_draft_credit_note_does_not_block(self, stocked, shop, warehouses, oil, user):
        """A draft has written nothing and can simply be deleted or re-pointed."""
        invoice = sales.post_sales_invoice(_sale(shop, warehouses.main, oil), user=user)
        _credit_note(shop, warehouses.main, oil, invoice, cartons=4)

        sales.cancel_sales_invoice(invoice, user=user, reason=REASON)
        assert invoice.status == DocumentStatus.CANCELLED

    def test_a_settled_cheque_blocks_its_payment_and_names_the_event(self, shop, accounts, user):
        payment = payments.post_payment(
            _receipt(shop, rupees=1_000_000, mode=PaymentMode.CHEQUE), user=user
        )
        event = payments.clear_cheque(payment, posting_date=JUNE, user=user)

        with pytest.raises(ChequeSettled) as caught:
            payments.cancel_payment(payment, user=user, reason=REASON)
        assert event.code in str(caught.value)

    def test_a_payment_with_allocations_is_not_blocked_by_them(
        self, stocked, shop, warehouses, oil, user
    ):
        """Deliberate, and the opposite of the invoice rule.

        An allocation writes no ledger row, and ``Payment.objects.live()``
        already drops a cancelled payment out of every figure — so the invoice
        goes back to open the moment this is cancelled, with nothing to unpick.
        """
        invoice = sales.post_sales_invoice(_sale(shop, warehouses.main, oil), user=user)
        payment = payments.post_payment(_receipt(shop, rupees=SALE_VALUE), user=user)
        payments.allocate_payment(payment, [(invoice, SALE_VALUE)], user=user)

        payments.cancel_payment(payment, user=user, reason=REASON)

        assert payment.status == DocumentStatus.CANCELLED
        assert invoice.outstanding_paisa == SALE_VALUE, "the bill is open again"


# ===========================================================================
# The reversal preview
# ===========================================================================
class TestReversalPreview:
    """What the cancel screen shows must be what the cancellation writes."""

    def test_it_matches_the_rows_the_cancellation_actually_writes(self, stocked, user):
        preview = preview_reversal(stocked)
        assert preview.ledger and preview.stock
        assert preview.balances

        purchasing.cancel_purchase_invoice(stocked, user=user, reason=REASON)

        written = LedgerEntry.objects.filter(voucher_code=stocked.code, is_reversal=True).order_by(
            "pk"
        )
        assert [(row.account_id, row.debit_paisa, row.credit_paisa) for row in written] == [
            (line.account.pk, line.debit_paisa, line.credit_paisa) for line in preview.ledger
        ]

        stock_rows = StockEntry.objects.filter(
            voucher_code=stocked.code, is_reversal=True
        ).order_by("pk")
        assert [(row.item_id, row.qty_base, row.value_paisa) for row in stock_rows] == [
            (line.item.pk, line.qty_base, line.value_paisa) for line in preview.stock
        ]

    def test_it_writes_nothing(self, stocked):
        before = LedgerEntry.objects.count(), StockEntry.objects.count()
        preview_reversal(stocked)
        assert (LedgerEntry.objects.count(), StockEntry.objects.count()) == before

    def test_it_is_empty_once_the_document_is_already_reversed(self, stocked, user):
        purchasing.cancel_purchase_invoice(stocked, user=user, reason=REASON)
        assert preview_reversal(stocked).is_empty

    def test_a_payment_previews_two_ledger_rows_and_no_stock(self, shop, accounts, user):
        payment = payments.post_payment(_receipt(shop, rupees=1_000_000), user=user)
        preview = preview_reversal(payment)
        assert len(preview.ledger) == 2
        assert preview.stock == []
        assert preview.balances


# ===========================================================================
# The round trip: post -> cancel -> amend -> post
# ===========================================================================
def _originals(code):
    """Every non-reversal row a voucher wrote, as comparable tuples."""
    ledger = [
        (row.pk, row.account_id, row.debit_paisa, row.credit_paisa, row.posting_date)
        for row in LedgerEntry.objects.filter(voucher_code=code, is_reversal=False).order_by("pk")
    ]
    stock = [
        (row.pk, row.item_id, row.qty_base, row.rate_paisa, row.value_paisa)
        for row in StockEntry.objects.filter(voucher_code=code, is_reversal=False).order_by("pk")
    ]
    return ledger, stock


class TestPurchaseInvoiceRoundTrip:
    def test_the_books_land_exactly_where_one_clean_posting_would_have(
        self, stocked, vendor, warehouses, oil, user
    ):
        before = _originals(stocked.code)
        assert party_balance(PartyType.VENDOR, vendor.pk).paisa == STOCK_VALUE

        purchasing.cancel_purchase_invoice(stocked, user=user, reason=REASON)
        assert party_balance(PartyType.VENDOR, vendor.pk).paisa == 0
        assert stock_balance(oil, warehouses.main) == (0, 0)

        amendment = purchasing.amend_purchase_invoice(stocked, user=user)
        purchasing.post_purchase_invoice(amendment, user=user)

        assert party_balance(PartyType.VENDOR, vendor.pk).paisa == STOCK_VALUE
        assert stock_balance(oil, warehouses.main) == (STOCK_PIECES, STOCK_VALUE)
        assert _originals(stocked.code) == before, "the original entries must survive untouched"

    def test_the_amendment_carries_the_lines_and_the_chain(self, stocked, user):
        purchasing.cancel_purchase_invoice(stocked, user=user, reason=REASON)
        amendment = purchasing.amend_purchase_invoice(stocked, user=user)

        assert amendment.code == f"{stocked.code}-1"
        assert amendment.status == DocumentStatus.DRAFT
        assert amendment.lines.count() == stocked.lines.count()
        assert amendment.total_paisa == stocked.total_paisa
        assert amendment.amended_from == stocked


class TestPurchaseReturnRoundTrip:
    @pytest.fixture
    def credit_note(self, stocked, vendor, warehouses, oil, user):
        document = purchasing.create_purchase_return(
            vendor=vendor, warehouse=warehouses.main, posting_date=MAY
        )
        purchasing.update_line(
            PRLine(document=document),
            item=oil,
            qty_input=10,
            unit_input=Unit.CARTON,
            rate_input_paisa=240_000,
        ).save()
        return purchasing.post_purchase_return(document, user=user)

    def test_the_books_land_exactly_where_one_clean_posting_would_have(
        self, credit_note, vendor, warehouses, oil, user
    ):
        before = _originals(credit_note.code)
        owed = STOCK_VALUE - PRETURN_VALUE
        held = (STOCK_PIECES - PRETURN_PIECES, STOCK_VALUE - PRETURN_VALUE)
        assert party_balance(PartyType.VENDOR, vendor.pk).paisa == owed
        assert stock_balance(oil, warehouses.main) == held

        purchasing.cancel_purchase_return(credit_note, user=user, reason=REASON)
        assert party_balance(PartyType.VENDOR, vendor.pk).paisa == STOCK_VALUE
        assert stock_balance(oil, warehouses.main) == (STOCK_PIECES, STOCK_VALUE)

        amendment = purchasing.amend_purchase_return(credit_note, user=user)
        purchasing.post_purchase_return(amendment, user=user)

        assert party_balance(PartyType.VENDOR, vendor.pk).paisa == owed
        assert stock_balance(oil, warehouses.main) == held
        assert _originals(credit_note.code) == before


class TestSalesInvoiceRoundTrip:
    @pytest.fixture
    def invoice(self, stocked, shop, warehouses, oil, user):
        return sales.post_sales_invoice(_sale(shop, warehouses.main, oil), user=user)

    def test_the_books_land_exactly_where_one_clean_posting_would_have(
        self, invoice, shop, warehouses, oil, user
    ):
        before = _originals(invoice.code)
        held = (STOCK_PIECES - SALE_PIECES, STOCK_VALUE - SALE_COST)
        assert party_balance(PartyType.CLIENT, shop.pk).paisa == SALE_VALUE
        assert stock_balance(oil, warehouses.main) == held

        sales.cancel_sales_invoice(invoice, user=user, reason=REASON)
        assert party_balance(PartyType.CLIENT, shop.pk).paisa == 0
        assert stock_balance(oil, warehouses.main) == (STOCK_PIECES, STOCK_VALUE), (
            "the stock must come back at exactly what it left at"
        )

        amendment = sales.amend_sales_invoice(invoice, user=user)
        assert amendment.lines.get().cogs_paisa == 0, "a draft has released nothing"

        sales.post_sales_invoice(amendment, user=user)
        assert party_balance(PartyType.CLIENT, shop.pk).paisa == SALE_VALUE
        assert stock_balance(oil, warehouses.main) == held
        assert amendment.lines.get().cogs_paisa == SALE_COST
        assert _originals(invoice.code) == before

    def test_the_cancelled_invoice_keeps_the_cost_it_recorded(self, invoice, user):
        """Rubbing it out would destroy the only record of what the sale cost."""
        sales.cancel_sales_invoice(invoice, user=user, reason=REASON)
        assert invoice.lines.get().cogs_paisa == SALE_COST


class TestSalesReturnRoundTrip:
    @pytest.fixture
    def note(self, stocked, shop, warehouses, oil, user):
        invoice = sales.post_sales_invoice(_sale(shop, warehouses.main, oil), user=user)
        return sales.post_sales_return(
            _credit_note(shop, warehouses.main, oil, invoice, cartons=4), user=user
        )

    def test_the_books_land_exactly_where_one_clean_posting_would_have(
        self, note, shop, warehouses, oil, user
    ):
        before = _originals(note.code)
        # 4 of the 10 cartons back: 48 pieces, credited Rs 10,000, restored to
        # stock at the Rs 9,600 they cost — its exact share of the invoice's cost.
        back_pieces, back_value, credited = 48, 48 * PIECE_COST, 1_000_000
        owed = SALE_VALUE - credited
        held = (STOCK_PIECES - SALE_PIECES + back_pieces, STOCK_VALUE - SALE_COST + back_value)

        assert party_balance(PartyType.CLIENT, shop.pk).paisa == owed
        assert stock_balance(oil, warehouses.main) == held

        sales.cancel_sales_return(note, user=user, reason=REASON)
        assert party_balance(PartyType.CLIENT, shop.pk).paisa == SALE_VALUE
        assert stock_balance(oil, warehouses.main) == (
            STOCK_PIECES - SALE_PIECES,
            STOCK_VALUE - SALE_COST,
        )

        amendment = sales.amend_sales_return(note, user=user)
        sales.post_sales_return(amendment, user=user)

        assert party_balance(PartyType.CLIENT, shop.pk).paisa == owed
        assert stock_balance(oil, warehouses.main) == held
        assert _originals(note.code) == before

    def test_the_amendment_still_names_the_invoice_it_is_against(self, note, user):
        sales.cancel_sales_return(note, user=user, reason=REASON)
        amendment = sales.amend_sales_return(note, user=user)
        assert amendment.against_invoice_id == note.against_invoice_id


class TestPaymentRoundTrip:
    @pytest.fixture
    def receipt(self, shop, accounts, user):
        return payments.post_payment(_receipt(shop, rupees=1_000_000), user=user)

    def test_the_books_land_exactly_where_one_clean_posting_would_have(
        self, receipt, shop, accounts, user
    ):
        from apps.accounting.services import account_balance

        before = _originals(receipt.code)
        assert party_balance(PartyType.CLIENT, shop.pk).paisa == -1_000_000
        assert account_balance(accounts.cash).paisa == 1_000_000

        payments.cancel_payment(receipt, user=user, reason=REASON)
        assert party_balance(PartyType.CLIENT, shop.pk).paisa == 0
        assert account_balance(accounts.cash).paisa == 0

        amendment = payments.amend_payment(receipt, user=user)
        payments.post_payment(amendment, user=user)

        assert party_balance(PartyType.CLIENT, shop.pk).paisa == -1_000_000
        assert account_balance(accounts.cash).paisa == 1_000_000
        assert _originals(receipt.code) == before

    def test_the_amendment_carries_the_allocations_across(
        self, stocked, shop, warehouses, oil, user
    ):
        """A payment cancelled to fix its date was still against the same bills."""
        invoice = sales.post_sales_invoice(_sale(shop, warehouses.main, oil), user=user)
        payment = payments.post_payment(_receipt(shop, rupees=SALE_VALUE), user=user)
        payments.allocate_payment(payment, [(invoice, SALE_VALUE)], user=user)

        payments.cancel_payment(payment, user=user, reason=REASON)
        amendment = payments.amend_payment(payment, user=user)
        payments.post_payment(amendment, user=user)

        assert amendment.allocated_paisa == SALE_VALUE
        assert invoice.outstanding_paisa == 0


class TestChequeEventRoundTrip:
    @pytest.fixture
    def cleared(self, shop, accounts, user):
        payment = payments.post_payment(
            _receipt(shop, rupees=1_000_000, mode=PaymentMode.CHEQUE), user=user
        )
        return payments.clear_cheque(payment, posting_date=JUNE, user=user)

    def test_the_books_land_exactly_where_one_clean_posting_would_have(
        self, cleared, accounts, user
    ):
        from apps.accounting import chart as coa
        from apps.accounting.services import account_balance

        drawer = accounts.by_code[coa.CHEQUES_IN_HAND]
        before = _originals(cleared.code)
        assert account_balance(accounts.bank).paisa == 1_000_000
        assert account_balance(drawer).paisa == 0

        payments.cancel_cheque_event(cleared, user=user, reason=REASON)
        assert account_balance(accounts.bank).paisa == 0
        assert account_balance(drawer).paisa == 1_000_000, "back in the drawer"
        cleared.payment.refresh_from_db()
        assert cleared.payment.cheque_status == "PENDING"

        amendment = payments.amend_cheque_event(cleared, user=user)
        payments.post_cheque_event(amendment, user=user)

        assert account_balance(accounts.bank).paisa == 1_000_000
        assert account_balance(drawer).paisa == 0
        assert _originals(cleared.code) == before

    def test_the_amendment_is_suffixed_off_the_root_code(self, cleared, user):
        payments.cancel_cheque_event(cleared, user=user, reason=REASON)
        amendment = payments.amend_cheque_event(cleared, user=user)
        assert amendment.code == f"{cleared.code}-1"


# ===========================================================================
# The chain and the timeline
# ===========================================================================
class TestChain:
    @pytest.fixture
    def three(self, stocked, user):
        """An original and two amendments, each cancelled in turn."""
        purchasing.cancel_purchase_invoice(stocked, user=user, reason=REASON)
        first = purchasing.amend_purchase_invoice(stocked, user=user)
        purchasing.post_purchase_invoice(first, user=user)
        purchasing.cancel_purchase_invoice(first, user=user, reason=REASON)
        second = purchasing.amend_purchase_invoice(first, user=user)
        return stocked, first, second

    def test_it_reads_oldest_first_from_any_link(self, three):
        original, first, second = three
        expected = [original.pk, first.pk, second.pk]

        for link in three:
            assert [document.pk for document in link.chain()] == expected

    def test_the_codes_suffix_the_root_never_the_previous_amendment(self, three):
        original, _first, second = three
        assert [document.code for document in original.chain()] == [
            original.code,
            f"{original.code}-1",
            f"{original.code}-2",
        ]
        assert second.amendment_no == 2

    def test_a_document_with_no_amendments_is_its_own_chain(self, stocked):
        assert [document.pk for document in stocked.chain()] == [stocked.pk]

    def test_next_amendment_is_none_until_one_is_made(self, stocked, user):
        assert stocked.next_amendment() is None
        purchasing.cancel_purchase_invoice(stocked, user=user, reason=REASON)
        assert stocked.next_amendment() is None
        amendment = purchasing.amend_purchase_invoice(stocked, user=user)
        assert stocked.next_amendment() == amendment


class TestTimeline:
    def test_a_draft_shows_created_and_a_posting_that_has_not_happened(
        self, vendor, warehouses, oil, accounts, user
    ):
        draft = _purchase(vendor, warehouses.main, oil, cartons=1, rupees=240_000)
        steps = {step.kind: step for step in draft.timeline()}

        assert set(steps) == {"created", "posted"}
        assert steps["created"].at is not None
        assert steps["posted"].at is None
        assert steps["posted"].is_done is False

    def test_posting_stamps_who_and_when(self, stocked, user):
        posted = {step.kind: step for step in stocked.timeline()}["posted"]
        assert posted.is_done
        assert posted.by == user
        assert posted.at == stocked.posted_at

    def test_cancelling_adds_the_reason_and_an_empty_amended_into(self, stocked, user):
        purchasing.cancel_purchase_invoice(stocked, user=user, reason=REASON)
        steps = {step.kind: step for step in stocked.timeline()}

        assert steps["cancelled"].by == user
        assert steps["cancelled"].note == REASON
        assert steps["amended_to"].document is None
        assert "Nothing has replaced" in steps["amended_to"].note

    def test_the_chain_links_both_ways(self, stocked, user):
        purchasing.cancel_purchase_invoice(stocked, user=user, reason=REASON)
        amendment = purchasing.amend_purchase_invoice(stocked, user=user)

        forward = {step.kind: step for step in stocked.timeline()}["amended_to"]
        backward = {step.kind: step for step in amendment.timeline()}["amended_from"]

        assert forward.document == amendment
        assert backward.document == stocked
        assert backward.note == REASON, "the reason travels forward to the replacement"


# ===========================================================================
# Cancelled documents: on every list, out of every figure
# ===========================================================================
class TestCancelledDocumentsAreVisibleButNotCounted:
    def test_live_leaves_cancelled_out_and_cancelled_finds_only_them(self, stocked, user):
        purchasing.cancel_purchase_invoice(stocked, user=user, reason=REASON)

        assert PurchaseInvoice.objects.count() == 1
        assert not PurchaseInvoice.objects.live().exists()
        assert list(PurchaseInvoice.objects.cancelled()) == [stocked]
        assert list(PurchaseInvoice.objects.for_report(include_cancelled=True)) == [stocked]

    def test_it_is_never_deleted(self, stocked, user):
        from apps.core.exceptions import DocumentImmutable

        purchasing.cancel_purchase_invoice(stocked, user=user, reason=REASON)
        with pytest.raises(DocumentImmutable):
            stocked.delete()
        assert PurchaseInvoice.objects.filter(pk=stocked.pk).exists()

    def test_it_still_appears_on_the_list_screen_with_no_filter(
        self, stocked, user, client, django_user_model
    ):
        purchasing.cancel_purchase_invoice(stocked, user=user, reason=REASON)
        # A Viewer: read-only, and that is the point — a cancelled document is
        # never hidden from a list screen (CLAUDE.md §5), including from
        # somebody whose whole access is looking at it.
        client.force_login(
            join_group(
                django_user_model.objects.create_user(
                    username="viewer", password="x", is_staff=True
                ),
                "Viewer",
            )
        )

        body = client.get("/purchasing/invoices/").content.decode()
        assert stocked.code in body, "a cancelled document is never hidden from a list"

    def test_the_detail_screen_carries_the_watermark(
        self, stocked, user, client, django_user_model
    ):
        # A Viewer: read-only, and that is the point — a cancelled document is
        # never hidden from a list screen (CLAUDE.md §5), including from
        # somebody whose whole access is looking at it.
        client.force_login(
            join_group(
                django_user_model.objects.create_user(
                    username="viewer", password="x", is_staff=True
                ),
                "Viewer",
            )
        )
        url = f"/purchasing/invoices/{stocked.pk}/"

        assert "doc-watermark" not in client.get(url).content.decode()

        purchasing.cancel_purchase_invoice(stocked, user=user, reason=REASON)
        body = client.get(url).content.decode()
        assert "doc-watermark" in body
        assert "CANCELLED" in body

    def test_the_cheque_register_leaves_a_cancelled_receipt_out_until_asked(
        self, shop, accounts, user
    ):
        from apps.payments.recovery import pending_cheques

        payment = payments.post_payment(
            _receipt(shop, rupees=1_000_000, mode=PaymentMode.CHEQUE), user=user
        )
        assert list(pending_cheques(as_of=JUNE)) == [payment]

        payments.cancel_payment(payment, user=user, reason=REASON)
        assert list(pending_cheques(as_of=JUNE)) == []
        assert list(pending_cheques(as_of=JUNE, include_cancelled=True)) == [payment]


# ===========================================================================
# History is for masters, and only for masters
# ===========================================================================
class TestMasterHistory:
    """django-simple-history covers the six masters and nothing else.

    Not documents: a POSTED document cannot be modified at all (CLAUDE.md §5),
    and every correction is already a reversing entry in the ledger under its
    own date and user (CLAUDE.md §3). A second audit log over documents would be
    a second version of the truth.
    """

    HISTORIED = [
        "masters.Item",
        "masters.Client",
        "masters.Vendor",
        "masters.Route",
        "masters.Seller",
        "accounting.Account",
    ]

    @pytest.mark.parametrize("label", HISTORIED)
    def test_the_master_has_history(self, label):
        model = django_apps.get_model(label)
        assert hasattr(model, "history"), f"{label} should record row history"

    def test_editing_an_item_records_what_it_used_to_be(self, oil):
        oil.sale_rate_paisa = 25_000
        oil.save()
        oil.sale_rate_paisa = 27_500
        oil.save()

        rates = list(oil.history.order_by("history_date").values_list("sale_rate_paisa", flat=True))
        assert rates == [0, 25_000, 27_500]

    def test_raising_a_credit_limit_is_recorded(self, shop):
        was = shop.credit_limit_paisa
        shop.credit_limit_paisa = was * 2
        shop.save()

        latest, previous = shop.history.all()[:2]
        assert previous.credit_limit_paisa == was
        assert latest.credit_limit_paisa == was * 2

    @pytest.mark.parametrize("model", document_models(), ids=lambda m: m._meta.label)
    def test_no_document_has_history(self, model):
        assert not hasattr(model, "history"), (
            f"{model._meta.label} must not carry HistoricalRecords — the ledger is "
            f"already its audit log (CLAUDE.md §3)."
        )

    @pytest.mark.parametrize("label", ["accounting.LedgerEntry", "accounting.StockEntry"])
    def test_no_ledger_has_history(self, label):
        """A row that can never change has no history to keep."""
        assert not hasattr(django_apps.get_model(label), "history")

    @pytest.mark.parametrize("label", HISTORIED)
    def test_the_admin_serves_the_history_screen(
        self, label, admin_client_logged_in, oil, shop, vendor, accounts
    ):
        """The bit that silently breaks: Unfold's ModelAdmin and SimpleHistoryAdmin.

        Get the inheritance order wrong and the page still renders — just
        without any history on it. This asks for the screen and checks the row's
        history came back with it.
        """
        model = django_apps.get_model(label)
        row = _some_row(model)

        url = f"/admin/{model._meta.app_label}/{model._meta.model_name}/{row.pk}/history/"
        response = admin_client_logged_in.get(url)

        assert response.status_code == 200
        assert model.history.model.objects.filter(id=row.pk).exists()
        assert str(row.pk) in response.content.decode()


def _some_row(model):
    """One saved row of a master **that has a historical record**.

    The second half is load-bearing and was not always true. A master seeded by
    a *data migration* has no history at all: the migration is handed
    ``apps.get_model()``'s historical model (see
    ``apps.accounting.chart.seed_chart_of_accounts``), and simple_history's
    ``post_save`` receiver is registered on the live model, not on that one. So
    the chart of accounts arrives in a fresh database with rows and no history.

    That is correct behaviour — nobody changed those rows, so there is nothing
    to record — but it is not what this test is about. Saving the row once gives
    it the historical record the admin screen is supposed to render.

    Without this, the test passed only on a reused test database where an
    earlier ``TransactionTestCase`` flush had wiped the migration's rows and the
    ``accounts`` fixture had re-seeded them through the live model.
    """
    existing = model.objects.first()
    if existing is not None:
        if not model.history.model.objects.filter(id=existing.pk).exists():
            existing.save()
        return existing
    from apps.masters.models import Route, Seller

    if model is Route:
        return Route.objects.create(code="R-99", name="Spot run")
    if model is Seller:
        return Seller.objects.create(code="S-99", name="Kashif Ali")
    raise AssertionError(f"no way to make a {model._meta.label} for this test")


# ===========================================================================
# The one thing amending must never allow
# ===========================================================================
class TestAmendOnlyFromCancelled:
    @pytest.mark.parametrize(
        "service",
        [
            purchasing.amend_purchase_invoice,
            purchasing.amend_purchase_return,
            sales.amend_sales_invoice,
            sales.amend_sales_return,
            payments.amend_payment,
            payments.amend_cheque_event,
        ],
        ids=lambda service: service.__name__,
    )
    def test_every_amend_refuses_a_posted_document(self, service, stocked, user):
        """Amending before the reversal would double-count the original.

        The document handed in here is the wrong *type* for most of these
        services on purpose: the refusal comes from ``build_amendment`` on the
        base class, so it must fire before anything looks at the subclass.
        """
        with pytest.raises(IllegalTransition, match="only a CANCELLED"):
            service(stocked, user=user)
