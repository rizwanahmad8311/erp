"""The credit limit: when a sale is refused, and who can post it anyway.

The rule is deliberately the plain one — what the client already owes, plus what
this invoice comes to, must not exceed their limit — and the two things that
make it trustworthy are tested here:

* **It is checked before anything is written.** A refusal must leave a clean
  DRAFT that can still be edited, not a half-posted invoice with stock gone.
* **The balance comes from the ledger.** Not from a cached field, not from the
  invoice headers. It counts every posted invoice, credit note and adjustment
  the client has ever had, because that is the only figure a credit decision can
  honestly be made on.

The message is tested too. "Over the limit" is useless to the person on the
counter; the limit, the balance and the overage are what they need to decide
whether to phone the owner.
"""

import datetime as dt

import pytest
from django.contrib.auth.models import Permission

from apps.accounting.enums import PartyType
from apps.accounting.models import LedgerEntry, StockEntry
from apps.accounting.services import party_balance
from apps.core.enums import DocumentStatus
from apps.core.money import Money, to_paisa
from apps.masters.enums import Unit
from apps.masters.models import Client, Item, Vendor
from apps.purchasing import services as purchasing
from apps.sales import services
from apps.sales.exceptions import CreditLimitExceeded
from apps.sales.models import SalesInvoice, SalesInvoiceLine

pytestmark = pytest.mark.django_db

APRIL = dt.date(2026, 4, 1)
MAY = dt.date(2026, 5, 1)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def oil(db):
    return Item.objects.create(
        code="OIL-1000", name="Cooking Oil 1L", carton_size=12, tax_rate_bp=0
    )


@pytest.fixture
def stocked(db, accounts, warehouses, oil, user):
    """1,200 pieces at Rs 200 — plenty, so stock is never what refuses a sale."""
    vendor = Vendor.objects.create(code="V-01", name="Dalda Foods")
    bill = purchasing.create_purchase_invoice(
        vendor=vendor, warehouse=warehouses.main, posting_date=APRIL
    )
    line = purchasing.update_line(
        purchasing.PurchaseInvoiceLine(document=bill),
        item=oil,
        qty_input=100,
        unit_input=Unit.CARTON,
        rate_input_paisa=to_paisa("2400"),
    )
    line.save()
    purchasing.post_purchase_invoice(bill, user=user)
    return bill


@pytest.fixture
def shop(db):
    """A Rs 10,000 limit. Small on purpose — every figure below is checkable."""
    return Client.objects.create(
        code="C-0001", name="Al-Madina Kiryana", credit_limit_paisa=to_paisa("10000")
    )


@pytest.fixture
def cash_only(db):
    """Limit of zero. The field's help text says that means no credit at all."""
    return Client.objects.create(code="C-0002", name="Walk-in", credit_limit_paisa=0)


@pytest.fixture
def override_user(django_user_model, db):
    operator = django_user_model.objects.create_user(username="supervisor", password="x")
    operator.user_permissions.add(
        Permission.objects.get(codename="override_credit_limit", content_type__app_label="sales")
    )
    return django_user_model.objects.get(pk=operator.pk)  # re-fetch: perms are cached


@pytest.fixture
def plain_user(django_user_model, db):
    return django_user_model.objects.create_user(username="counter", password="x")


def invoice_for(shop, warehouses, *, rupees, oil, qty_input=1, posting_date=APRIL):
    """A DRAFT invoice for a round number of rupees, tax-free for easy arithmetic."""
    document = services.create_sales_invoice(
        client=shop, warehouse=warehouses.main, posting_date=posting_date
    )
    line = services.update_line(
        SalesInvoiceLine(document=document),
        item=oil,
        qty_input=qty_input,
        unit_input=Unit.CARTON,
        rate_input_paisa=to_paisa(rupees),
    )
    line.save()
    # What every real caller does after touching a line — the entry screen on
    # each change, the posting service before it checks anything.
    services.recalculate_totals(document)
    return document


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------
class TestTheRule:
    def test_an_invoice_inside_the_limit_posts(self, stocked, shop, warehouses, oil, user):
        document = invoice_for(shop, warehouses, rupees="9000", oil=oil)
        services.post_sales_invoice(document, user=user)
        assert document.status == DocumentStatus.POSTED

    def test_an_invoice_exactly_on_the_limit_posts(self, stocked, shop, warehouses, oil, user):
        """The rule is "must not *exceed*", so landing on the number is fine."""
        document = invoice_for(shop, warehouses, rupees="10000", oil=oil)
        services.post_sales_invoice(document, user=user)
        assert document.status == DocumentStatus.POSTED
        assert party_balance(PartyType.CLIENT, shop.pk) == Money(to_paisa("10000"))

    def test_one_paisa_over_is_refused(self, stocked, shop, warehouses, oil, user):
        document = invoice_for(shop, warehouses, rupees="10000.01", oil=oil)
        with pytest.raises(CreditLimitExceeded):
            services.post_sales_invoice(document, user=user)

    def test_the_balance_counts_what_is_already_owed(self, stocked, shop, warehouses, oil, user):
        """Two invoices that each fit, but do not fit together."""
        first = invoice_for(shop, warehouses, rupees="6000", oil=oil)
        services.post_sales_invoice(first, user=user)

        second = invoice_for(shop, warehouses, rupees="6000", oil=oil, posting_date=MAY)
        with pytest.raises(CreditLimitExceeded):
            services.post_sales_invoice(second, user=user)

    def test_a_credit_note_frees_the_headroom_up_again(self, stocked, shop, warehouses, oil, user):
        """The balance is aggregated from the ledger, so a return releases room
        the moment it posts — nothing has to be told about it."""
        first = invoice_for(shop, warehouses, rupees="9000", oil=oil)
        services.post_sales_invoice(first, user=user)

        note = services.create_sales_return(
            client=shop, warehouse=warehouses.main, posting_date=MAY, against_invoice=first
        )
        from apps.sales.models import SalesReturnLine

        line = services.update_line(
            SalesReturnLine(document=note),
            item=oil,
            qty_input=1,
            unit_input=Unit.CARTON,
            rate_input_paisa=to_paisa("9000"),
        )
        line.save()
        services.post_sales_return(note, user=user)

        assert party_balance(PartyType.CLIENT, shop.pk) == Money.zero()
        second = invoice_for(shop, warehouses, rupees="9000", oil=oil, posting_date=MAY)
        services.post_sales_invoice(second, user=user)
        assert second.status == DocumentStatus.POSTED

    def test_the_tax_counts_towards_the_limit(self, stocked, shop, warehouses, user):
        """It is money the client owes, so it is money against their limit."""
        taxed = Item.objects.create(
            code="TEA-190", name="Tea 190g", carton_size=12, tax_rate_bp=1750
        )
        purchase = purchasing.create_purchase_invoice(
            vendor=Vendor.objects.get(code="V-01"),
            warehouse=warehouses.main,
            posting_date=APRIL,
        )
        line = purchasing.update_line(
            purchasing.PurchaseInvoiceLine(document=purchase),
            item=taxed,
            qty_input=10,
            unit_input=Unit.CARTON,
            rate_input_paisa=to_paisa("1200"),
        )
        line.save()
        purchasing.post_purchase_invoice(purchase, user=user)

        # Rs 9,000 of goods is inside the limit; Rs 9,000 + 17.5% is not.
        document = invoice_for(shop, warehouses, rupees="9000", oil=taxed)
        assert document.subtotal_paisa == to_paisa("9000")
        assert document.total_paisa > to_paisa("10000")
        with pytest.raises(CreditLimitExceeded):
            services.post_sales_invoice(document, user=user)

    def test_a_zero_limit_client_gets_no_credit_at_all(
        self, stocked, cash_only, warehouses, oil, user
    ):
        """``credit_limit_paisa = 0`` means what the master says it means.

        Every invoice for such a client needs the override — which is the point
        of marking them cash-only.
        """
        document = invoice_for(cash_only, warehouses, rupees="100", oil=oil)
        with pytest.raises(CreditLimitExceeded):
            services.post_sales_invoice(document, user=user)


# ---------------------------------------------------------------------------
# Nothing is written when it refuses
# ---------------------------------------------------------------------------
class TestARefusalWritesNothing:
    @pytest.fixture
    def refused(self, stocked, shop, warehouses, oil, user):
        document = invoice_for(shop, warehouses, rupees="50000", oil=oil)
        with pytest.raises(CreditLimitExceeded):
            services.post_sales_invoice(document, user=user)
        return document

    def test_the_invoice_is_still_a_draft(self, refused):
        refused.refresh_from_db()
        assert refused.status == DocumentStatus.DRAFT

    def test_no_ledger_row_was_written(self, refused):
        assert not LedgerEntry.objects.filter(voucher_code=refused.code).exists()

    def test_no_stock_moved(self, refused, oil, warehouses):
        from apps.accounting.services import stock_balance

        assert not StockEntry.objects.filter(voucher_code=refused.code).exists()
        assert stock_balance(oil, warehouses.main).qty_base == 1200

    def test_the_client_still_owes_nothing(self, refused, shop):
        assert party_balance(PartyType.CLIENT, shop.pk) == Money.zero()

    def test_the_draft_is_still_editable_so_it_can_be_cut_down(self, refused, oil, user):
        """The whole point of refusing before writing: the operator fixes it."""
        line = refused.lines.first()
        services.update_line(
            line, item=oil, qty_input=1, unit_input=Unit.CARTON, rate_input_paisa=to_paisa("5000")
        )
        line.save()
        services.post_sales_invoice(refused, user=user)
        assert refused.status == DocumentStatus.POSTED


# ---------------------------------------------------------------------------
# The message
# ---------------------------------------------------------------------------
class TestTheMessage:
    """ "Over the limit" is useless on the counter. These three numbers are not."""

    @pytest.fixture
    def caught(self, stocked, shop, warehouses, oil, user):
        first = invoice_for(shop, warehouses, rupees="8000", oil=oil)
        services.post_sales_invoice(first, user=user)

        second = invoice_for(shop, warehouses, rupees="5000", oil=oil, posting_date=MAY)
        with pytest.raises(CreditLimitExceeded) as caught:
            services.post_sales_invoice(second, user=user)
        return caught.value

    def test_it_shows_the_limit(self, caught):
        assert "10,000.00" in str(caught)
        assert caught.limit_paisa == to_paisa("10000")

    def test_it_shows_the_current_balance(self, caught):
        assert "8,000.00" in str(caught)
        assert caught.balance_paisa == to_paisa("8000")

    def test_it_shows_what_this_invoice_adds(self, caught):
        assert "5,000.00" in str(caught)
        assert caught.total_paisa == to_paisa("5000")

    def test_it_shows_the_overage(self, caught):
        assert "3,000.00" in str(caught)
        assert caught.overage_paisa == to_paisa("3000")
        assert caught.would_owe_paisa == to_paisa("13000")

    def test_it_names_the_client(self, caught):
        assert "Al-Madina Kiryana" in str(caught)
        assert "C-0001" in str(caught)

    def test_it_says_what_to_do_about_it(self, caught):
        assert "override credit limit" in str(caught)


# ---------------------------------------------------------------------------
# Override
# ---------------------------------------------------------------------------
class TestOverride:
    def test_the_permission_exists_on_the_invoice_model(self):
        assert Permission.objects.filter(
            codename="override_credit_limit", content_type__app_label="sales"
        ).exists()

    def test_a_user_with_the_permission_can_post_over_the_limit(
        self, stocked, shop, warehouses, oil, override_user
    ):
        document = invoice_for(shop, warehouses, rupees="50000", oil=oil)
        services.post_sales_invoice(document, user=override_user, override_credit_limit=True)

        assert document.status == DocumentStatus.POSTED
        assert party_balance(PartyType.CLIENT, shop.pk) == Money(to_paisa("50000"))

    def test_asking_for_the_override_without_the_permission_is_still_refused(
        self, stocked, shop, warehouses, oil, plain_user
    ):
        """The flag comes off a form, so it is not trusted on its own."""
        document = invoice_for(shop, warehouses, rupees="50000", oil=oil)
        with pytest.raises(CreditLimitExceeded):
            services.post_sales_invoice(document, user=plain_user, override_credit_limit=True)
        assert document.status == DocumentStatus.DRAFT

    def test_the_permission_alone_does_not_skip_the_check(
        self, stocked, shop, warehouses, oil, override_user
    ):
        """Holding it is not the same as using it — the override is deliberate."""
        document = invoice_for(shop, warehouses, rupees="50000", oil=oil)
        with pytest.raises(CreditLimitExceeded):
            services.post_sales_invoice(document, user=override_user)

    def test_it_makes_no_difference_to_an_invoice_inside_the_limit(
        self, stocked, shop, warehouses, oil, plain_user
    ):
        document = invoice_for(shop, warehouses, rupees="9000", oil=oil)
        services.post_sales_invoice(document, user=plain_user, override_credit_limit=True)
        assert document.status == DocumentStatus.POSTED

    def test_a_caller_with_no_user_is_trusted(self, stocked, shop, warehouses, oil):
        """A management command or a data migration is already trusted code.

        Nothing reachable from a browser gets in this way — a request always has
        a user attached.
        """
        document = invoice_for(shop, warehouses, rupees="50000", oil=oil)
        services.post_sales_invoice(document, user=None, override_credit_limit=True)
        assert document.status == DocumentStatus.POSTED


# ---------------------------------------------------------------------------
# The status object the screen reads
# ---------------------------------------------------------------------------
class TestCreditStatus:
    def test_it_reports_the_headroom_left(self, stocked, shop, warehouses, oil, user):
        first = invoice_for(shop, warehouses, rupees="4000", oil=oil)
        services.post_sales_invoice(first, user=user)

        draft = invoice_for(shop, warehouses, rupees="3000", oil=oil, posting_date=MAY)
        status = services.credit_status(draft)

        assert status.balance_paisa == to_paisa("4000")
        assert status.headroom_paisa == to_paisa("6000")
        assert status.document_paisa == to_paisa("3000")
        assert status.would_owe_paisa == to_paisa("7000")
        assert status.is_within_limit is True
        assert status.overage_paisa == to_paisa("-3000")

    def test_headroom_never_goes_negative(self, stocked, shop, warehouses, oil, override_user):
        document = invoice_for(shop, warehouses, rupees="50000", oil=oil)
        services.post_sales_invoice(document, user=override_user, override_credit_limit=True)

        draft = invoice_for(shop, warehouses, rupees="100", oil=oil, posting_date=MAY)
        status = services.credit_status(draft)
        assert status.balance_paisa == to_paisa("50000")
        assert status.headroom_paisa == 0
        assert status.is_within_limit is False

    def test_it_reads_the_ledger_not_the_invoice_headers(
        self, stocked, shop, warehouses, oil, user
    ):
        """Cancel a posted invoice and the balance follows the ledger, not the
        pile of documents that still exist."""
        first = invoice_for(shop, warehouses, rupees="9000", oil=oil)
        services.post_sales_invoice(first, user=user)
        services.cancel_sales_invoice(first, user=user)

        assert SalesInvoice.objects.filter(pk=first.pk).exists()  # still there
        draft = invoice_for(shop, warehouses, rupees="9000", oil=oil, posting_date=MAY)
        assert services.credit_status(draft).balance_paisa == 0
        services.post_sales_invoice(draft, user=user)
        assert draft.status == DocumentStatus.POSTED
