"""The sales entry screen — the primary UI of this system.

Four things are tested harder than the rest, because they are what "usable on a
counter all day" actually means:

* **The autocompletes find the right rows** — clients on code, name *or phone*;
  items on code or name, each carrying what is on hand.
* **Picking an item sets the row up server-side** — unit defaulted to CTN when
  the item is cartoned, rate converted to that unit, and the caret handed to
  Qty.
* **The client never computes money.** There is JavaScript on this page, unlike
  the purchasing screen, so there is a test that reads it and fails the build if
  it ever learns arithmetic.
* **The credit position is on the screen before you post**, not discovered by
  posting.
"""

import datetime as dt
import re
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.auth.models import Permission
from django.urls import reverse

from apps.accounting.models import LedgerEntry, StockEntry
from apps.core.enums import DocumentStatus
from apps.core.money import to_paisa
from apps.masters.enums import Unit
from apps.masters.models import Client, Item, Route, Seller, Vendor
from apps.purchasing import services as purchasing
from apps.sales import services
from apps.sales.models import SalesInvoice, SalesInvoiceLine, SalesReturn
from tests.conftest import grant_cancel

pytestmark = pytest.mark.django_db

APRIL = dt.date(2026, 4, 1)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
#: A reason long enough for the cancel form — see apps.core.forms.MIN_CANCEL_REASON.
CANCEL_REASON = "Keyed twice by mistake"


@pytest.fixture
def operator(django_user_model, db):
    user = django_user_model.objects.create_user(username="counter", password="x", is_staff=True)
    return grant_cancel(user, SalesInvoice, SalesReturn)


@pytest.fixture
def staff_client(client, operator):
    client.force_login(operator)
    return client


@pytest.fixture
def route(db):
    return Route.objects.create(code="R-01", name="Saddar & City")


@pytest.fixture
def seller(db):
    return Seller.objects.create(code="S-01", name="Imran Qureshi")


@pytest.fixture
def shop(db, route, seller):
    return Client.objects.create(
        code="C-0001",
        name="Al-Madina Kiryana",
        phone="0300-2214477",
        route=route,
        seller=seller,
        credit_limit_paisa=to_paisa("50000"),
        credit_days=15,
    )


@pytest.fixture
def oil(db):
    """Twelve to a carton, so CARTON is the sensible default unit."""
    return Item.objects.create(
        code="OIL-1000",
        name="Cooking Oil 1L",
        carton_size=12,
        tax_rate_bp=1750,
        sale_rate_paisa=to_paisa("250"),
    )


@pytest.fixture
def rice(db):
    """Not cartoned. The unit toggle must default to PIECE for this one."""
    return Item.objects.create(
        code="RICE-25", name="Basmati Rice 25kg", carton_size=1, sale_rate_paisa=to_paisa("7850")
    )


@pytest.fixture
def stocked(db, accounts, warehouses, oil, rice, user):
    vendor = Vendor.objects.create(code="V-01", name="Dalda Foods")
    bill = purchasing.create_purchase_invoice(
        vendor=vendor, warehouse=warehouses.main, posting_date=APRIL
    )
    for item, qty, unit, rate in (
        (oil, 10, Unit.CARTON, "2400"),
        (rice, 20, Unit.PIECE, "7000"),
    ):
        line = purchasing.update_line(
            purchasing.PurchaseInvoiceLine(document=bill),
            item=item,
            qty_input=qty,
            unit_input=unit,
            rate_input_paisa=to_paisa(rate),
        )
        line.save()
    purchasing.post_purchase_invoice(bill, user=user)
    return bill


@pytest.fixture
def draft(db, stocked, shop, warehouses):
    return services.create_sales_invoice(client=shop, warehouse=warehouses.main, posting_date=APRIL)


def url(name, document, **kwargs):
    return reverse(f"sales:{name}", kwargs={"slug": "invoices", "pk": document.pk, **kwargs})


def line_payload(item, **overrides):
    return {
        "item": item.pk,
        "qty_input": 2,
        "unit_input": Unit.CARTON,
        "rate_input": "3000",
        "discount": "",
        **overrides,
    }


# ---------------------------------------------------------------------------
# List and create
# ---------------------------------------------------------------------------
class TestList:
    def test_it_lists_documents(self, staff_client, draft):
        response = staff_client.get(reverse("sales:list", kwargs={"slug": "invoices"}))
        assert response.status_code == 200
        assert draft.code in response.content.decode()

    def test_it_searches_by_client_phone(self, staff_client, draft):
        response = staff_client.get(
            reverse("sales:list", kwargs={"slug": "invoices"}), {"q": "2214477"}
        )
        assert draft.code in response.content.decode()

    def test_an_unknown_document_type_is_a_404(self, staff_client):
        assert (
            staff_client.get(reverse("sales:list", kwargs={"slug": "widgets"})).status_code == 404
        )

    def test_it_needs_a_login(self, client):
        response = client.get(reverse("sales:list", kwargs={"slug": "invoices"}))
        assert response.status_code == 302


class TestCreate:
    def test_submitting_the_header_allocates_a_code_and_defaults_the_beat(
        self, staff_client, stocked, shop, warehouses, route, seller
    ):
        response = staff_client.post(
            reverse("sales:create", kwargs={"slug": "invoices"}),
            {
                "client": shop.pk,
                "warehouse": warehouses.main.pk,
                "posting_date": "2026-04-01",
                "route": "",
                "seller": "",
                "due_date": "",
                "remarks": "",
            },
        )
        invoice = SalesInvoice.objects.get()
        assert invoice.code == "SI-2026-000001"
        assert invoice.route == route  # defaulted from the client
        assert invoice.seller == seller
        assert invoice.due_date == APRIL + dt.timedelta(days=15)
        assert response.url == url("detail", invoice)

    def test_the_beat_can_be_overridden_on_the_way_in(
        self, staff_client, stocked, shop, warehouses
    ):
        """A booker covering someone else's route says so at entry time."""
        cover = Seller.objects.create(code="S-02", name="Bilal Ahmed")
        other = Route.objects.create(code="R-02", name="Malir")
        staff_client.post(
            reverse("sales:create", kwargs={"slug": "invoices"}),
            {
                "client": shop.pk,
                "warehouse": warehouses.main.pk,
                "posting_date": "2026-04-01",
                "route": other.pk,
                "seller": cover.pk,
                "due_date": "",
                "remarks": "",
            },
        )
        invoice = SalesInvoice.objects.get()
        assert invoice.route == other
        assert invoice.seller == cover

    def test_opening_the_form_burns_no_document_number(self, staff_client, accounts):
        from apps.core.models import DocumentSequence

        staff_client.get(reverse("sales:create", kwargs={"slug": "invoices"}))
        assert not DocumentSequence.objects.filter(prefix="SI").exists()


# ---------------------------------------------------------------------------
# Autocomplete
# ---------------------------------------------------------------------------
class TestClientAutocomplete:
    """Code, name **or phone** — a shopkeeper on the line gives a number."""

    @pytest.mark.parametrize("query", ["C-0001", "madina", "MADINA", "2214477", "0300"])
    def test_it_finds_the_shop(self, staff_client, shop, query):
        response = staff_client.get(reverse("sales:client-search"), {"q": query})
        assert response.status_code == 200
        assert "Al-Madina Kiryana" in response.content.decode()

    def test_it_shows_the_beat_so_the_default_is_visible_before_committing(
        self, staff_client, shop
    ):
        body = staff_client.get(reverse("sales:client-search"), {"q": "madina"}).content.decode()
        assert "R-01" in body
        assert "Imran Qureshi" in body

    def test_an_empty_query_returns_nothing_rather_than_everything(self, staff_client, shop):
        body = staff_client.get(reverse("sales:client-search"), {"q": ""}).content.decode()
        assert "Al-Madina" not in body

    def test_a_miss_says_so(self, staff_client, shop):
        body = staff_client.get(reverse("sales:client-search"), {"q": "zzzz"}).content.decode()
        assert "No client matches" in body

    def test_inactive_clients_are_not_offered(self, staff_client, shop):
        shop.is_active = False
        shop.save()
        body = staff_client.get(reverse("sales:client-search"), {"q": "madina"}).content.decode()
        assert "Al-Madina Kiryana" not in body


class TestItemAutocomplete:
    @pytest.mark.parametrize("query", ["OIL-1000", "OIL", "cooking", "Oil 1L"])
    def test_it_finds_the_item_on_code_or_name(self, staff_client, draft, oil, query):
        response = staff_client.get(url("item-search", draft), {"q": query})
        assert response.status_code == 200
        assert "OIL-1000" in response.content.decode()

    def test_each_hit_shows_what_is_on_hand(self, staff_client, draft, oil):
        """ "Is there any left" is the next question; answering it here saves a
        keystroke on a screen used a thousand times a day."""
        body = staff_client.get(url("item-search", draft), {"q": "OIL"}).content.decode()
        assert "10 ctn" in body  # 120 pieces at 12 to the carton

    def test_it_shows_the_carton_size(self, staff_client, draft, oil):
        body = staff_client.get(url("item-search", draft), {"q": "OIL"}).content.decode()
        assert "12/ctn" in body

    def test_a_miss_says_so(self, staff_client, draft):
        body = staff_client.get(url("item-search", draft), {"q": "zzzz"}).content.decode()
        assert "No item matches" in body


class TestPickingAnItem:
    """The row arrives already filled in — that is the whole point of the pick."""

    def test_the_unit_defaults_to_carton_for_a_cartoned_item(self, staff_client, draft, oil):
        body = staff_client.get(
            reverse(
                "sales:item-pick",
                kwargs={"slug": "invoices", "pk": draft.pk, "item_pk": oil.pk},
            )
        ).content.decode()
        assert re.search(r'<option value="CARTON"\s+selected>', body)

    def test_the_unit_defaults_to_piece_for_a_loose_item(self, staff_client, draft, rice):
        body = staff_client.get(
            reverse(
                "sales:item-pick",
                kwargs={"slug": "invoices", "pk": draft.pk, "item_pk": rice.pk},
            )
        ).content.decode()
        assert re.search(r'<option value="PIECE"\s+selected>', body)

    def test_the_rate_is_converted_to_the_unit_on_the_server(self, staff_client, draft, oil):
        """Rs 250 a piece at twelve to the carton is Rs 3,000 a carton.

        Computed in Python. The browser is never asked to multiply money.
        """
        body = staff_client.get(
            reverse(
                "sales:item-pick",
                kwargs={"slug": "invoices", "pk": draft.pk, "item_pk": oil.pk},
            )
        ).content.decode()
        assert 'value="3000.00"' in body

    def test_a_loose_item_keeps_its_per_piece_rate(self, staff_client, draft, rice):
        body = staff_client.get(
            reverse(
                "sales:item-pick",
                kwargs={"slug": "invoices", "pk": draft.pk, "item_pk": rice.pk},
            )
        ).content.decode()
        assert 'value="7850.00"' in body

    def test_the_caret_is_handed_to_quantity(self, staff_client, draft, oil):
        """ "Enter moves to quantity", done without the browser knowing anything
        about items or money: the swapped-in row carries data-autofocus."""
        body = staff_client.get(
            reverse(
                "sales:item-pick",
                kwargs={"slug": "invoices", "pk": draft.pk, "item_pk": oil.pk},
            )
        ).content.decode()
        assert "data-autofocus" in body
        assert 'name="qty_input"' in body

    def test_it_shows_the_stock_behind_the_item(self, staff_client, draft, oil):
        body = staff_client.get(
            reverse(
                "sales:item-pick",
                kwargs={"slug": "invoices", "pk": draft.pk, "item_pk": oil.pk},
            )
        ).content.decode()
        assert "In stock:" in body
        assert "10 ctn" in body


# ---------------------------------------------------------------------------
# The grid
# ---------------------------------------------------------------------------
class TestLineEntry:
    def test_adding_a_line_returns_the_whole_grid(self, staff_client, draft, oil):
        response = staff_client.post(url("line-add", draft), line_payload(oil))
        assert response.status_code == 200
        assert draft.lines.count() == 1
        assert 'id="grid"' in response.content.decode()

    def test_the_line_is_stored_from_what_was_typed(self, staff_client, draft, oil):
        staff_client.post(url("line-add", draft), line_payload(oil))
        line = draft.lines.get()
        assert line.qty_input == 2
        assert line.unit_input == Unit.CARTON
        assert line.qty_base == 24
        assert line.amount_paisa == 600_000

    def test_the_totals_are_recomputed_server_side(self, staff_client, draft, oil):
        response = staff_client.post(url("line-add", draft), line_payload(oil))
        draft.refresh_from_db()
        assert draft.subtotal_paisa == 600_000
        assert draft.tax_paisa == 105_000
        assert draft.total_paisa == 705_000
        assert "7,050.00" in response.content.decode()

    def test_each_line_shows_live_stock(self, staff_client, draft, oil):
        body = staff_client.post(url("line-add", draft), line_payload(oil)).content.decode()
        assert "In stock" in body or "10 ctn" in body

    def test_a_line_for_more_than_is_held_is_flagged(self, staff_client, draft, oil):
        """Flagged, not blocked — the stock may arrive before it is posted, and
        the stock ledger refuses it at post time if it has not."""
        body = staff_client.post(
            url("line-add", draft), line_payload(oil, qty_input=50)
        ).content.decode()
        assert "posting will be refused" in body.lower() or "!" in body
        assert draft.lines.count() == 1

    def test_a_carton_entry_for_a_loose_item_is_refused(self, staff_client, draft, rice):
        response = staff_client.post(url("line-add", draft), line_payload(rice))
        assert response.status_code == 422
        assert "not sold by the carton" in response.content.decode()
        assert draft.lines.count() == 0

    def test_a_line_can_be_removed(self, staff_client, draft, oil):
        staff_client.post(url("line-add", draft), line_payload(oil))
        line = draft.lines.get()
        staff_client.post(url("line-delete", draft, line_pk=line.pk))
        assert draft.lines.count() == 0

    def test_lines_cannot_be_added_to_a_posted_document(self, staff_client, draft, oil, user):
        staff_client.post(url("line-add", draft), line_payload(oil))
        services.post_sales_invoice(draft, user=user)
        assert staff_client.post(url("line-add", draft), line_payload(oil)).status_code == 404


class TestLinePreview:
    def test_it_computes_the_row_without_saving_it(self, staff_client, draft, oil):
        response = staff_client.post(url("line-preview", draft), line_payload(oil))
        body = response.content.decode()
        assert draft.lines.count() == 0
        assert "6,000.00" in body  # the line amount
        assert "2 ctn" in body
        assert "1,050.00" in body  # the tax

    def test_it_warns_when_the_row_outruns_the_stock(self, staff_client, draft, oil):
        body = staff_client.post(
            url("line-preview", draft), line_payload(oil, qty_input=50)
        ).content.decode()
        assert "posting will be refused" in body

    def test_an_incomplete_row_previews_nothing_rather_than_guessing(self, staff_client, draft):
        response = staff_client.post(url("line-preview", draft), {"qty_input": 2})
        assert response.status_code == 200
        assert response.content.decode().strip() == ""


# ---------------------------------------------------------------------------
# The client never computes money
# ---------------------------------------------------------------------------
class TestTheClientNeverComputesMoney:
    """Unlike the purchasing screen, this one ships JavaScript. So it gets read.

    A browser adding up paisa in IEEE doubles is CLAUDE.md §1 broken in the one
    place nobody thinks to look, and it breaks quietly.
    """

    #: Anything that turns text into a number, or names the stored unit of money.
    BANNED = ("parseInt", "parseFloat", "Number(", "toFixed", "paisa", "Math.round")

    def _js(self) -> str:
        source = Path(settings.BASE_DIR) / "static" / "src" / "js" / "entry-grid.js"
        return source.read_text(encoding="utf-8")

    def test_the_entry_script_does_no_arithmetic_on_money(self):
        body = self._js()
        # Strip block comments: the header explains the rule and names the
        # banned tokens, which would otherwise trip the check it describes.
        code = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
        offenders = [needle for needle in self.BANNED if needle in code]
        assert not offenders, (
            f"static/src/js/entry-grid.js must not compute money: found {offenders}"
        )

    def test_the_shipped_copy_matches_the_source(self):
        """static/dist is what production serves and it is committed, so the two
        must not drift — `make js` copies, it does not compile."""
        base = Path(settings.BASE_DIR)
        source = (base / "static" / "src" / "js" / "entry-grid.js").read_bytes()
        shipped = (base / "static" / "dist" / "js" / "entry-grid.js").read_bytes()
        assert source == shipped

    def test_the_entry_screen_has_no_inline_script(self, staff_client, draft, oil):
        staff_client.post(url("line-add", draft), line_payload(oil))
        body = staff_client.get(url("detail", draft)).content.decode()
        inline = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", body, re.S)
        assert not [block for block in inline if block.strip()], (
            f"The entry screen must not compute money in the browser: {inline}"
        )

    def test_the_partials_return_html_not_json(self, staff_client, draft, oil):
        response = staff_client.post(url("line-add", draft), line_payload(oil))
        assert response["Content-Type"].startswith("text/html")

    def test_amounts_arrive_already_formatted(self, staff_client, draft, oil):
        body = staff_client.post(url("line-add", draft), line_payload(oil)).content.decode()
        assert "6,000.00" in body
        assert "600000" not in body


# ---------------------------------------------------------------------------
# The posting strip
# ---------------------------------------------------------------------------
class TestPostingStrip:
    @pytest.fixture
    def with_line(self, staff_client, draft, oil):
        staff_client.post(url("line-add", draft), line_payload(oil))
        draft.refresh_from_db()
        return draft

    def test_it_is_pinned_to_the_bottom(self, staff_client, with_line):
        """On the screen somebody uses all day, the total must never be
        somewhere you have to scroll to find."""
        body = staff_client.get(url("detail", with_line)).content.decode()
        assert "sticky bottom-0" in body

    def test_it_previews_the_general_ledger(self, staff_client, with_line):
        body = staff_client.get(url("detail", with_line)).content.decode()
        assert "General ledger preview" in body
        for name in ("Accounts Receivable", "Sales", "Tax Payable", "Cost of Goods Sold"):
            assert name in body

    def test_the_preview_is_what_actually_posts(self, staff_client, with_line, user):
        previewed = {
            (gl.account.code, gl.debit_paisa, gl.credit_paisa)
            for gl in services.build_invoice_gl(
                with_line, cogs_paisa=services.preview_cogs_paisa(with_line)
            )
        }
        services.post_sales_invoice(with_line, user=user)
        posted = {
            (entry.account.code, entry.debit_paisa, entry.credit_paisa)
            for entry in LedgerEntry.objects.filter(voucher_code=with_line.code)
        }
        assert previewed == posted

    def test_it_says_whether_the_preview_balances(self, staff_client, with_line):
        body = staff_client.get(url("detail", with_line)).content.decode()
        assert "Balanced" in body
        assert "OUT OF BALANCE" not in body

    def test_it_shows_the_credit_position_before_posting(self, staff_client, with_line):
        """Discovered on the screen, not by trying to post."""
        body = staff_client.get(url("detail", with_line)).content.decode()
        assert "Credit — Al-Madina Kiryana" in body
        assert "50,000.00" in body  # the limit
        assert "Headroom left" in body

    def test_it_shows_the_keyboard_route(self, staff_client, with_line):
        body = staff_client.get(url("detail", with_line)).content.decode()
        assert "Keyboard" in body
        assert "Alt+P" in body


# ---------------------------------------------------------------------------
# The credit limit, from the screen
# ---------------------------------------------------------------------------
class TestCreditLimitOnScreen:
    @pytest.fixture
    def over_limit(self, staff_client, draft, oil):
        """Rs 50,000 limit, and a line that comes to more than that."""
        staff_client.post(url("line-add", draft), line_payload(oil, qty_input=8, rate_input="8000"))
        draft.refresh_from_db()
        return draft

    def test_the_strip_says_it_is_over(self, staff_client, over_limit):
        body = staff_client.get(url("detail", over_limit)).content.decode()
        assert "Would owe (over)" in body
        assert "Over by" in body

    def test_posting_is_refused_and_the_message_shows_the_figures(self, staff_client, over_limit):
        response = staff_client.post(url("post", over_limit), follow=True)
        over_limit.refresh_from_db()
        body = response.content.decode()

        assert over_limit.status == DocumentStatus.DRAFT
        assert "over their credit limit" in body
        assert "50,000.00" in body  # the limit
        assert not LedgerEntry.objects.filter(voucher_code=over_limit.code).exists()
        assert not StockEntry.objects.filter(voucher_code=over_limit.code).exists()

    def test_an_operator_without_the_permission_is_not_offered_the_override(
        self, staff_client, over_limit
    ):
        body = staff_client.get(url("detail", over_limit)).content.decode()
        assert "needs someone with the override permission" in body
        assert 'name="override_credit_limit"' not in body

    def test_a_supervisor_is_offered_it(self, client, django_user_model, over_limit):
        supervisor = django_user_model.objects.create_user(
            username="supervisor", password="x", is_staff=True
        )
        supervisor.user_permissions.add(
            Permission.objects.get(
                codename="override_credit_limit", content_type__app_label="sales"
            )
        )
        client.force_login(django_user_model.objects.get(pk=supervisor.pk))

        body = client.get(url("detail", over_limit)).content.decode()
        assert 'name="override_credit_limit"' in body
        assert "Post over the limit" in body

    def test_ticking_it_without_the_permission_still_refuses(self, staff_client, over_limit):
        """A value off a form is not a permission."""
        staff_client.post(url("post", over_limit), {"override_credit_limit": "1"})
        over_limit.refresh_from_db()
        assert over_limit.status == DocumentStatus.DRAFT

    def test_a_supervisor_ticking_it_posts(self, client, django_user_model, over_limit):
        supervisor = django_user_model.objects.create_user(
            username="supervisor", password="x", is_staff=True
        )
        supervisor.user_permissions.add(
            Permission.objects.get(
                codename="override_credit_limit", content_type__app_label="sales"
            )
        )
        client.force_login(django_user_model.objects.get(pk=supervisor.pk))

        client.post(url("post", over_limit), {"override_credit_limit": "1"})
        over_limit.refresh_from_db()
        assert over_limit.status == DocumentStatus.POSTED


# ---------------------------------------------------------------------------
# Lifecycle from the screen
# ---------------------------------------------------------------------------
class TestLifecycleActions:
    @pytest.fixture
    def with_line(self, staff_client, draft, oil):
        staff_client.post(url("line-add", draft), line_payload(oil))
        draft.refresh_from_db()
        return draft

    def test_posting_writes_both_ledgers(self, staff_client, with_line):
        staff_client.post(url("post", with_line))
        with_line.refresh_from_db()
        assert with_line.status == DocumentStatus.POSTED
        assert LedgerEntry.objects.filter(voucher_code=with_line.code).count() == 5
        assert StockEntry.objects.filter(voucher_code=with_line.code).count() == 1

    def test_posting_captures_the_cost_onto_the_line(self, staff_client, with_line):
        staff_client.post(url("post", with_line))
        assert with_line.lines.get().cogs_paisa == 480_000  # 24 pieces at Rs 200

    def test_the_cancel_screen_previews_the_reversing_entries_before_confirming(
        self, staff_client, with_line
    ):
        staff_client.post(url("post", with_line))
        before = LedgerEntry.objects.filter(voucher_code=with_line.code).count()

        response = staff_client.get(url("cancel", with_line))
        body = response.content.decode()

        assert response.status_code == 200
        assert "Reversing general ledger entries" in body
        assert "Reversing stock entries" in body
        with_line.refresh_from_db()
        assert with_line.status == DocumentStatus.POSTED
        assert LedgerEntry.objects.filter(voucher_code=with_line.code).count() == before

    def test_cancelling_reverses_both_ledgers(self, staff_client, with_line):
        staff_client.post(url("post", with_line))
        staff_client.post(url("cancel", with_line), {"reason": CANCEL_REASON})
        with_line.refresh_from_db()
        assert with_line.status == DocumentStatus.CANCELLED
        assert with_line.cancel_reason == CANCEL_REASON
        entries = LedgerEntry.objects.filter(voucher_code=with_line.code)
        assert sum(e.debit_paisa - e.credit_paisa for e in entries) == 0

    def test_a_short_reason_is_refused_and_nothing_is_reversed(self, staff_client, with_line):
        staff_client.post(url("post", with_line))
        response = staff_client.post(url("cancel", with_line), {"reason": "oops"})

        with_line.refresh_from_db()
        assert response.status_code == 422
        assert with_line.status == DocumentStatus.POSTED
        assert not LedgerEntry.objects.filter(
            voucher_code=with_line.code, is_reversal=True
        ).exists()

    def test_amending_opens_the_new_draft(self, staff_client, with_line):
        staff_client.post(url("post", with_line))
        staff_client.post(url("cancel", with_line), {"reason": CANCEL_REASON})
        response = staff_client.post(url("amend", with_line))
        amendment = SalesInvoice.objects.get(amended_from=with_line)
        assert response.url == url("detail", amendment)
        assert amendment.lines.get().cogs_paisa == 0

    def test_a_posted_screen_shows_no_entry_row(self, staff_client, with_line):
        staff_client.post(url("post", with_line))
        body = staff_client.get(url("detail", with_line)).content.decode()
        assert "Add line" not in body
        assert "Cancel &amp; reverse" in body

    def test_the_screen_carries_the_timeline_and_gains_the_watermark(self, staff_client, with_line):
        staff_client.post(url("post", with_line))
        posted = staff_client.get(url("detail", with_line)).content.decode()
        assert "Posted" in posted
        assert "doc-watermark" not in posted

        staff_client.post(url("cancel", with_line), {"reason": CANCEL_REASON})
        cancelled = staff_client.get(url("detail", with_line)).content.decode()

        assert "doc-watermark" in cancelled
        assert CANCEL_REASON in cancelled, "the reason belongs on the timeline"
        assert "Not yet amended" in cancelled


# ---------------------------------------------------------------------------
# The credit note screen is the same screen
# ---------------------------------------------------------------------------
class TestCreditNoteScreen:
    def test_it_serves_the_return_type_too(self, staff_client, stocked, shop, warehouses):
        note = services.create_sales_return(
            client=shop, warehouse=warehouses.main, posting_date=APRIL
        )
        response = staff_client.get(
            reverse("sales:detail", kwargs={"slug": "returns", "pk": note.pk})
        )
        assert response.status_code == 200
        assert "Credit note" in response.content.decode()
        assert note.code.startswith("SR-")

    def test_it_shows_no_credit_panel_because_a_return_never_busts_a_limit(
        self, staff_client, stocked, shop, warehouses
    ):
        note = services.create_sales_return(
            client=shop, warehouse=warehouses.main, posting_date=APRIL
        )
        body = staff_client.get(
            reverse("sales:detail", kwargs={"slug": "returns", "pk": note.pk})
        ).content.decode()
        assert "Credit —" not in body

    def test_it_says_where_the_stock_will_be_valued_from(
        self, staff_client, stocked, shop, warehouses, oil, user
    ):
        invoice = services.create_sales_invoice(
            client=shop, warehouse=warehouses.main, posting_date=APRIL
        )
        line = services.update_line(
            SalesInvoiceLine(document=invoice),
            item=oil,
            qty_input=2,
            unit_input=Unit.CARTON,
            rate_input_paisa=to_paisa("3000"),
        )
        line.save()
        services.post_sales_invoice(invoice, user=user)

        note = services.create_sales_return(
            client=shop,
            warehouse=warehouses.main,
            posting_date=APRIL,
            against_invoice=invoice,
        )
        staff_client.post(
            reverse("sales:line-add", kwargs={"slug": "returns", "pk": note.pk}),
            line_payload(oil, qty_input=1),
        )
        body = staff_client.get(
            reverse("sales:detail", kwargs={"slug": "returns", "pk": note.pk})
        ).content.decode()
        assert f"Stock comes back at what {invoice.code} recorded it costing." in body
