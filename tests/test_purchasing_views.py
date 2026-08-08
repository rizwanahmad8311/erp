"""The purchase entry screens.

The one property worth testing hardest is the one that is easiest to lose: **the
client never computes money**. Every figure on the page arrives as rendered
HTML, computed by ``apps.purchasing.services`` on the server. A regression here
looks like a feature — "the totals update instantly now!" — right up until a
browser's float arithmetic puts a bill a paisa out.

So these tests assert on the rendered HTML rather than on a JSON payload, and
there is a test that fails if the entry screen ever starts shipping raw numbers
for a script to add up.
"""

import datetime as dt
import re

import pytest
from django.urls import reverse

from apps.accounting.models import LedgerEntry, StockEntry
from apps.core.enums import DocumentStatus
from apps.masters.enums import Unit
from apps.masters.models import Item, Vendor
from apps.purchasing import services
from apps.purchasing.models import PurchaseInvoice, PurchaseInvoiceLine

pytestmark = pytest.mark.django_db

APRIL = dt.date(2026, 4, 1)


@pytest.fixture
def staff_client(client, django_user_model, db):
    operator = django_user_model.objects.create_user(
        username="entry", password="entry-pass", is_staff=True
    )
    client.force_login(operator)
    return client


@pytest.fixture
def vendor(db):
    return Vendor.objects.create(code="V-01", name="Unilever Pakistan Ltd")


@pytest.fixture
def tea(db):
    """Twenty-four to a carton: the packing where rates do not divide."""
    return Item.objects.create(code="TEA-190", name="Tea 190g", carton_size=24, tax_rate_bp=1750)


@pytest.fixture
def draft(db, accounts, warehouses, vendor):
    return services.create_purchase_invoice(
        vendor=vendor, warehouse=warehouses.main, posting_date=APRIL, vendor_bill_no="INV-1"
    )


def url(name, document, **kwargs):
    return reverse(f"purchasing:{name}", kwargs={"slug": "invoices", "pk": document.pk, **kwargs})


# ---------------------------------------------------------------------------
# List and create
# ---------------------------------------------------------------------------
class TestList:
    def test_it_lists_documents(self, staff_client, draft):
        response = staff_client.get(reverse("purchasing:list", kwargs={"slug": "invoices"}))
        assert response.status_code == 200
        assert draft.code in response.content.decode()

    def test_it_filters_by_status(self, staff_client, draft, tea, user):
        posted = services.create_purchase_invoice(
            vendor=draft.vendor, warehouse=draft.warehouse, posting_date=APRIL
        )
        line = services.update_line(
            PurchaseInvoiceLine(document=posted),
            item=tea,
            qty_input=1,
            unit_input=Unit.CARTON,
            rate_input_paisa=250000,
        )
        line.save()
        services.post_purchase_invoice(posted, user=user)

        response = staff_client.get(
            reverse("purchasing:list", kwargs={"slug": "invoices"}), {"status": "POSTED"}
        )
        body = response.content.decode()
        assert posted.code in body
        assert draft.code not in body

    def test_an_unknown_document_type_is_a_404(self, staff_client):
        response = staff_client.get(reverse("purchasing:list", kwargs={"slug": "widgets"}))
        assert response.status_code == 404

    def test_it_needs_a_login(self, client, draft):
        response = client.get(reverse("purchasing:list", kwargs={"slug": "invoices"}))
        assert response.status_code == 302


class TestCreate:
    def test_the_form_renders(self, staff_client, accounts, warehouses, vendor):
        response = staff_client.get(reverse("purchasing:create", kwargs={"slug": "invoices"}))
        assert response.status_code == 200

    def test_submitting_it_allocates_a_code_and_opens_the_grid(
        self, staff_client, warehouses, vendor, accounts
    ):
        response = staff_client.post(
            reverse("purchasing:create", kwargs={"slug": "invoices"}),
            {
                "vendor": vendor.pk,
                "warehouse": warehouses.main.pk,
                "posting_date": "2026-04-01",
                "vendor_bill_no": "INV-777",
                "vendor_bill_date": "2026-04-01",
                "remarks": "",
            },
        )
        invoice = PurchaseInvoice.objects.get(vendor_bill_no="INV-777")
        assert invoice.code == "PI-2026-000001"
        assert invoice.status == DocumentStatus.DRAFT
        assert response.status_code == 302
        assert response.url == url("detail", invoice)

    def test_opening_the_form_burns_no_document_number(self, staff_client, accounts):
        """A code is allocated on submit, never on page load (CLAUDE.md §5)."""
        from apps.core.models import DocumentSequence

        staff_client.get(reverse("purchasing:create", kwargs={"slug": "invoices"}))
        assert not DocumentSequence.objects.filter(prefix="PI").exists()


# ---------------------------------------------------------------------------
# The entry grid
# ---------------------------------------------------------------------------
class TestLineEntry:
    def line_payload(self, item, **overrides):
        return {
            "item": item.pk,
            "qty_input": 10,
            "unit_input": Unit.CARTON,
            "rate_input": "2500",
            "discount": "",
            **overrides,
        }

    def test_adding_a_line_returns_the_whole_grid(self, staff_client, draft, tea):
        response = staff_client.post(url("line-add", draft), self.line_payload(tea))
        assert response.status_code == 200

        body = response.content.decode()
        assert draft.lines.count() == 1
        # The grid comes back with the line and the recomputed totals together.
        assert "TEA-190" in body
        assert 'id="grid"' in body

    def test_the_line_is_stored_from_what_was_typed(self, staff_client, draft, tea):
        staff_client.post(url("line-add", draft), self.line_payload(tea))
        line = draft.lines.get()
        assert line.qty_input == 10
        assert line.unit_input == Unit.CARTON
        assert line.qty_base == 240
        assert line.amount_paisa == 2_500_000  # exactly 10 x Rs 2,500
        assert line.rate_paisa == 10417  # derived, rounded once

    def test_the_totals_are_recomputed_server_side(self, staff_client, draft, tea):
        response = staff_client.post(url("line-add", draft), self.line_payload(tea))
        body = response.content.decode()
        draft.refresh_from_db()

        assert draft.subtotal_paisa == 2_500_000
        assert draft.tax_paisa == 437_500
        assert draft.total_paisa == 2_937_500
        # Rendered, not shipped as a number for a script to format.
        assert "29,375.00" in body

    def test_the_rounded_rate_is_flagged_on_screen(self, staff_client, draft, tea):
        """So nobody reads the stock card and thinks the bill is wrong."""
        response = staff_client.post(url("line-add", draft), self.line_payload(tea))
        assert "rate x the base quantity does not land back" in response.content.decode()

    def test_an_invalid_line_comes_back_with_the_error_and_saves_nothing(
        self, staff_client, draft, tea
    ):
        response = staff_client.post(url("line-add", draft), self.line_payload(tea, qty_input=0))
        assert response.status_code == 422
        assert draft.lines.count() == 0

    def test_a_carton_entry_for_a_loose_item_is_refused(self, staff_client, draft):
        """It almost always means the wrong item was picked."""
        rice = Item.objects.create(code="RICE-25", name="Rice 25kg", carton_size=1)
        response = staff_client.post(url("line-add", draft), self.line_payload(rice, qty_input=5))
        assert response.status_code == 422
        assert "not sold by the carton" in response.content.decode()
        assert draft.lines.count() == 0

    def test_a_line_can_be_removed(self, staff_client, draft, tea):
        staff_client.post(url("line-add", draft), self.line_payload(tea))
        line = draft.lines.get()

        response = staff_client.post(url("line-delete", draft, line_pk=line.pk))
        assert response.status_code == 200
        assert draft.lines.count() == 0
        draft.refresh_from_db()
        assert draft.total_paisa == 0

    def test_lines_cannot_be_added_to_a_posted_document(self, staff_client, draft, tea, user):
        staff_client.post(url("line-add", draft), self.line_payload(tea))
        services.post_purchase_invoice(draft, user=user)

        response = staff_client.post(url("line-add", draft), self.line_payload(tea))
        assert response.status_code == 404
        assert draft.lines.count() == 1


class TestLinePreview:
    def test_it_computes_the_row_without_saving_it(self, staff_client, draft, tea):
        response = staff_client.post(
            url("line-preview", draft),
            {
                "item": tea.pk,
                "qty_input": 10,
                "unit_input": Unit.CARTON,
                "rate_input": "2500",
                "discount": "",
            },
        )
        body = response.content.decode()
        assert response.status_code == 200
        assert draft.lines.count() == 0  # nothing was written

        assert "25,000.00" in body  # the line amount, formatted on the server
        assert "10 ctn" in body  # the quantity, through masters.fmt_qty
        assert "104.17" in body  # the derived per-base-unit rate

    def test_it_says_when_the_rate_is_rounded(self, staff_client, draft, tea):
        response = staff_client.post(
            url("line-preview", draft),
            {
                "item": tea.pk,
                "qty_input": 10,
                "unit_input": Unit.CARTON,
                "rate_input": "2500",
                "discount": "",
            },
        )
        assert "rate rounded" in response.content.decode()

    def test_an_exact_rate_is_not_flagged(self, staff_client, draft):
        oil = Item.objects.create(code="OIL", name="Oil 1L", carton_size=12, tax_rate_bp=1750)
        response = staff_client.post(
            url("line-preview", draft),
            {
                "item": oil.pk,
                "qty_input": 10,
                "unit_input": Unit.CARTON,
                "rate_input": "2400",
                "discount": "",
            },
        )
        assert "rate rounded" not in response.content.decode()

    def test_an_incomplete_row_previews_nothing_rather_than_guessing(self, staff_client, draft):
        response = staff_client.post(url("line-preview", draft), {"qty_input": 10})
        assert response.status_code == 200
        assert response.content.decode().strip() == ""


class TestTheClientNeverComputesMoney:
    """The rule this screen exists to keep.

    A browser doing paisa arithmetic in floats is CLAUDE.md §1 broken in the one
    place nobody thinks to look, so the server sends finished HTML and the page
    ships no arithmetic of its own.
    """

    def test_the_entry_screen_has_no_javascript_arithmetic(self, staff_client, draft, tea):
        staff_client.post(url("line-add", draft), self.payload(tea))
        body = staff_client.get(url("detail", draft)).content.decode()

        # Everything between <script> tags on this page should be a src= include
        # of the vendored htmx, and nothing else.
        inline = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", body, re.S)
        assert not [block for block in inline if block.strip()], (
            f"The entry screen must not compute money in the browser: {inline}"
        )

    def test_the_partials_return_html_not_json(self, staff_client, draft, tea):
        response = staff_client.post(url("line-add", draft), self.payload(tea))
        assert response["Content-Type"].startswith("text/html")

    def test_amounts_arrive_already_formatted(self, staff_client, draft, tea):
        """Rs 25,000.00, not 2500000 with a note to divide by a hundred."""
        response = staff_client.post(url("line-add", draft), self.payload(tea))
        body = response.content.decode()
        assert "25,000.00" in body
        assert "2500000" not in body

    def payload(self, item):
        return {
            "item": item.pk,
            "qty_input": 10,
            "unit_input": Unit.CARTON,
            "rate_input": "2500",
            "discount": "",
        }


# ---------------------------------------------------------------------------
# The posting strip
# ---------------------------------------------------------------------------
class TestPostingStrip:
    @pytest.fixture
    def with_line(self, staff_client, draft, tea):
        staff_client.post(
            url("line-add", draft),
            {
                "item": tea.pk,
                "qty_input": 10,
                "unit_input": Unit.CARTON,
                "rate_input": "2500",
                "discount": "",
            },
        )
        # The view recalculated the totals in the database; this instance is
        # still holding the zeros it was created with.
        draft.refresh_from_db()
        return draft

    def test_it_previews_the_general_ledger(self, staff_client, with_line):
        body = staff_client.get(url("detail", with_line)).content.decode()
        assert "General ledger preview" in body
        assert "Accounts Payable" in body
        assert "Inventory" in body
        assert "Tax Payable" in body

    def test_the_preview_is_what_actually_posts(self, staff_client, with_line, user):
        """Built by the posting service itself, so it cannot drift."""
        previewed = {
            (gl.account.code, gl.debit_paisa, gl.credit_paisa)
            for gl in services.build_invoice_gl(with_line)
        }
        services.post_purchase_invoice(with_line, user=user)
        posted = {
            (entry.account.code, entry.debit_paisa, entry.credit_paisa)
            for entry in LedgerEntry.objects.filter(voucher_code=with_line.code)
        }
        assert previewed == posted

    def test_it_says_whether_the_preview_balances(self, staff_client, with_line):
        body = staff_client.get(url("detail", with_line)).content.decode()
        assert "Balanced" in body
        assert "OUT OF BALANCE" not in body

    def test_an_empty_draft_offers_nothing_to_post(self, staff_client, draft):
        body = staff_client.get(url("detail", draft)).content.decode()
        assert "Add a line to see what this will post" in body
        assert "disabled" in body


# ---------------------------------------------------------------------------
# Lifecycle from the screen
# ---------------------------------------------------------------------------
class TestLifecycleActions:
    @pytest.fixture
    def with_line(self, staff_client, draft, tea):
        staff_client.post(
            url("line-add", draft),
            {
                "item": tea.pk,
                "qty_input": 10,
                "unit_input": Unit.CARTON,
                "rate_input": "2500",
                "discount": "",
            },
        )
        return draft

    def test_posting_writes_both_ledgers(self, staff_client, with_line):
        response = staff_client.post(url("post", with_line))
        with_line.refresh_from_db()

        assert response.status_code == 302
        assert with_line.status == DocumentStatus.POSTED
        assert LedgerEntry.objects.filter(voucher_code=with_line.code).count() == 3
        assert StockEntry.objects.filter(voucher_code=with_line.code).count() == 1

    def test_posting_an_empty_draft_reports_the_error(self, staff_client, draft):
        response = staff_client.post(url("post", draft), follow=True)
        draft.refresh_from_db()
        assert draft.status == DocumentStatus.DRAFT
        assert "no lines" in response.content.decode()

    def test_cancelling_reverses_both_ledgers(self, staff_client, with_line):
        staff_client.post(url("post", with_line))
        staff_client.post(url("cancel", with_line), {"reason": "Wrong supplier"})
        with_line.refresh_from_db()

        assert with_line.status == DocumentStatus.CANCELLED
        assert with_line.cancel_reason == "Wrong supplier"
        entries = LedgerEntry.objects.filter(voucher_code=with_line.code)
        assert sum(e.debit_paisa - e.credit_paisa for e in entries) == 0

    def test_cancelling_a_paid_invoice_shows_which_payments_block_it(
        self, staff_client, with_line, monkeypatch
    ):
        staff_client.post(url("post", with_line))
        monkeypatch.setattr(
            services,
            "payment_allocations",
            lambda document: [services.Allocation("PV-2026-000012", 100_000)],
        )

        response = staff_client.post(url("cancel", with_line), follow=True)
        with_line.refresh_from_db()
        assert with_line.status == DocumentStatus.POSTED
        assert "PV-2026-000012" in response.content.decode()

    def test_amending_opens_the_new_draft(self, staff_client, with_line):
        staff_client.post(url("post", with_line))
        staff_client.post(url("cancel", with_line))
        response = staff_client.post(url("amend", with_line))

        amendment = PurchaseInvoice.objects.get(amended_from=with_line)
        assert amendment.status == DocumentStatus.DRAFT
        assert amendment.lines.count() == 1
        assert response.url == url("detail", amendment)

    def test_a_draft_can_be_deleted_from_the_screen(self, staff_client, draft):
        response = staff_client.post(url("delete", draft))
        assert response.status_code == 302
        assert not PurchaseInvoice.objects.filter(pk=draft.pk).exists()

    def test_a_posted_document_cannot_be_deleted_from_the_screen(self, staff_client, with_line):
        staff_client.post(url("post", with_line))
        response = staff_client.post(url("delete", with_line), follow=True)
        assert PurchaseInvoice.objects.filter(pk=with_line.pk).exists()
        assert "cannot be" in response.content.decode()

    def test_a_posted_screen_shows_no_entry_row(self, staff_client, with_line):
        staff_client.post(url("post", with_line))
        body = staff_client.get(url("detail", with_line)).content.decode()
        assert "Add line" not in body
        assert "Cancel &amp; reverse" in body


# ---------------------------------------------------------------------------
# The return screen is the same screen
# ---------------------------------------------------------------------------
class TestReturnScreen:
    def test_it_serves_the_return_type_too(self, staff_client, accounts, warehouses, vendor):
        credit_note = services.create_purchase_return(
            vendor=vendor, warehouse=warehouses.main, posting_date=APRIL
        )
        response = staff_client.get(
            reverse("purchasing:detail", kwargs={"slug": "returns", "pk": credit_note.pk})
        )
        assert response.status_code == 200
        assert "Purchase return" in response.content.decode()
        assert credit_note.code.startswith("PR-")

    def test_its_preview_explains_that_stock_leaves_at_cost(
        self, staff_client, accounts, warehouses, vendor, tea, user
    ):
        stocking = services.create_purchase_invoice(
            vendor=vendor, warehouse=warehouses.main, posting_date=APRIL
        )
        line = services.update_line(
            PurchaseInvoiceLine(document=stocking),
            item=tea,
            qty_input=10,
            unit_input=Unit.CARTON,
            rate_input_paisa=250_000,
        )
        line.save()
        services.post_purchase_invoice(stocking, user=user)

        credit_note = services.create_purchase_return(
            vendor=vendor, warehouse=warehouses.main, posting_date=APRIL
        )
        staff_client.post(
            reverse("purchasing:line-add", kwargs={"slug": "returns", "pk": credit_note.pk}),
            {
                "item": tea.pk,
                "qty_input": 2,
                "unit_input": Unit.CARTON,
                "rate_input": "2500",
                "discount": "",
            },
        )
        body = staff_client.get(
            reverse("purchasing:detail", kwargs={"slug": "returns", "pk": credit_note.pk})
        ).content.decode()

        assert "moving average" in body
        assert "Estimated at today" in body
