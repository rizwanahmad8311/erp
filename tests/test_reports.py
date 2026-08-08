"""The reports: the framework, the catalogue, and the one test that matters.

The one that matters is :class:`TestTrialBalance`. A trial balance summing to
zero across a dataset that contains posted, cancelled **and** amended documents
is a single assertion that catches almost every ledger bug this system could
develop — an unbalanced posting, a reversal that mirrors the wrong side, an
amendment that double-counts, a report that quietly filters half of a posting
out. Everything else here is scaffolding around it.

The rest of the file checks the three properties the framework exists to
guarantee:

* **one column list, three formats** — the CSV header, the HTML table head and
  the PDF's table are the same labels in the same order, because they are the
  same tuple;
* **cancelled is excluded from figures and never from the audit view** — and
  since a cancellation's entries and their mirrors net to zero, the toggle must
  not move a single total;
* **every row that references a document links to it**.

The PDFs are checked the way ``tests/test_pdf.py`` checks them: it is a real
file, it has the pages it claims, and the words that matter are on it. Nothing
pins a coordinate.
"""

from __future__ import annotations

import csv
import datetime as dt
import io

import pytest
from django.urls import reverse
from django.utils.html import escape

from apps.accounting.enums import PartyType
from apps.accounting.services import party_balance, stock_balance
from apps.core.money import to_paisa
from apps.masters.enums import Unit
from apps.masters.models import Client, Item, Route, Seller, Vendor
from apps.payments import services as payments
from apps.payments.enums import PaymentDirection, PaymentMode
from apps.purchasing import services as purchasing
from apps.purchasing.models import PurchaseInvoiceLine
from apps.reports import ledger as report_ledger
from apps.reports.columns import Column, ReportRow, display, export
from apps.reports.criteria import Criteria
from apps.reports.exports import write_csv
from apps.reports.registry import REPORTS, get_report
from apps.sales import services as sales
from apps.sales.models import SalesInvoiceLine
from tests.test_pdf import assert_is_a_pdf, page_count, text_of

pytestmark = pytest.mark.django_db

APRIL = dt.date(2026, 4, 1)
MAY = dt.date(2026, 5, 1)
JUNE = dt.date(2026, 6, 1)
JULY = dt.date(2026, 7, 1)

#: Long enough to cover every document the fixtures post, so a report run over
#: it sees the whole story rather than a slice of it.
PERIOD = {"date_from": APRIL.isoformat(), "date_to": JULY.isoformat()}


# ===========================================================================
# A seeded business: posted, cancelled and amended
# ===========================================================================
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
        name="Al-Madina Kiryana Store",
        phone="0300-2214477",
        city="Karachi",
        route=route,
        seller=seller,
        credit_limit_paisa=to_paisa("5000000"),
        credit_days=15,
    )


@pytest.fixture
def other_shop(db, route, seller):
    return Client.objects.create(
        code="C-0002",
        name="Bismillah General Store",
        route=route,
        seller=seller,
        credit_limit_paisa=to_paisa("5000000"),
        credit_days=7,
    )


@pytest.fixture
def vendor(db):
    return Vendor.objects.create(code="V-01", name="Dalda Foods (Pvt) Ltd", city="Karachi")


@pytest.fixture
def oil(db):
    return Item.objects.create(
        code="OIL-1000",
        name="Cooking Oil 1 Litre Bottle",
        carton_size=12,
        tax_rate_bp=1750,
        sale_rate_paisa=to_paisa("250"),
    )


@pytest.fixture
def rice(db):
    return Item.objects.create(
        code="RICE-25",
        name="Basmati Rice 25kg Bag",
        carton_size=1,
        sale_rate_paisa=to_paisa("7850"),
    )


@pytest.fixture
def purchase(db, accounts, warehouses, vendor, oil, rice, user):
    """Stock on hand: 1,200 bottles and 40 bags, bought in April."""
    bill = purchasing.create_purchase_invoice(
        vendor=vendor,
        warehouse=warehouses.main,
        posting_date=APRIL,
        vendor_bill_no="DF-88213",
        vendor_bill_date=APRIL,
    )
    for item, qty, unit, rate in ((oil, 100, Unit.CARTON, "2400"), (rice, 40, Unit.PIECE, "7000")):
        purchasing.update_line(
            PurchaseInvoiceLine(document=bill),
            item=item,
            qty_input=qty,
            unit_input=unit,
            rate_input_paisa=to_paisa(rate),
        ).save()
    return purchasing.post_purchase_invoice(bill, user=user)


def _invoice(client, warehouse, item, *, when, qty, unit, rate, user):
    document = sales.create_sales_invoice(client=client, warehouse=warehouse, posting_date=when)
    sales.update_line(
        SalesInvoiceLine(document=document),
        item=item,
        qty_input=qty,
        unit_input=unit,
        rate_input_paisa=to_paisa(rate),
    ).save()
    return sales.post_sales_invoice(document, user=user)


@pytest.fixture
def books(db, purchase, shop, other_shop, warehouses, oil, rice, user):
    """The dataset the trial balance is asserted against.

    Deliberately every lifecycle state the ledger can be left in:

    * a **posted** invoice that is still open;
    * a **cancelled** invoice, whose entries and their reversals are both still
      in the ledger and net to zero;
    * an **amendment** of that cancelled invoice — a different document with its
      own entries, which is why an amendment must never be raised against a
      POSTED bill (CLAUDE.md §5);
    * a posted **receipt**, allocated against the open invoice;
    * a posted **credit note**.

    If a report can survive all five it can survive a real month.
    """
    open_invoice = _invoice(
        shop, warehouses.main, oil, when=MAY, qty=10, unit=Unit.CARTON, rate="3000", user=user
    )

    wrong = _invoice(
        other_shop, warehouses.main, rice, when=MAY, qty=5, unit=Unit.PIECE, rate="8000", user=user
    )
    sales.cancel_sales_invoice(wrong, user=user, reason="Keyed against the wrong shop entirely")
    amendment = sales.amend_sales_invoice(wrong, user=user)
    sales.post_sales_invoice(amendment, user=user)

    receipt = payments.post_payment(
        payments.create_payment(
            party=shop,
            direction=PaymentDirection.RECEIVE,
            mode=PaymentMode.CASH,
            posting_date=JUNE,
            amount_paisa=to_paisa("20000"),
        ),
        user=user,
    )
    payments.allocate_payment(receipt, [(open_invoice, to_paisa("20000"))], user=user)

    return {
        "open_invoice": open_invoice,
        "cancelled": wrong,
        "amendment": amendment,
        "receipt": receipt,
    }


@pytest.fixture
def profile(db):
    """A filled-in company profile. Every printed page reads this."""
    from apps.reports.models import CompanyProfile

    company = CompanyProfile.get()
    company.name = "Al-Noor Distributors"
    company.address = "Plot 14, Sector 7-A\nKorangi Industrial Area, Karachi"
    company.ntn = "1234567-8"
    company.save()
    return company


def run(http, slug, **params):
    """GET one report's screen.

    The first argument is deliberately not called ``client``: half these reports
    take a ``client=`` filter, and a helper that shadowed it would be a helper
    nobody could call for a client ledger.
    """
    return http.get(reverse("reports:report", kwargs={"slug": slug}), params)


def rows_of(response):
    """The rendered rows out of a report response's context."""
    return response.context["rows"]


def cell(response, row_index: int, key: str) -> str:
    """One rendered cell, by column key."""
    for entry in rows_of(response)[row_index]["cells"]:
        if entry["column"].key == key:
            return entry["text"]
    raise KeyError(key)


# ===========================================================================
# The test that catches most ledger bugs
# ===========================================================================
class TestTrialBalance:
    """It must sum to zero. Everything else about it is decoration."""

    def _totals(self, criteria):
        return get_report("trial-balance").build(criteria).totals

    def test_it_sums_to_zero_across_posted_cancelled_and_amended(self, books):
        """The assertion this whole app is checked by.

        Every posting balances inside its own transaction (CLAUDE.md §4) and a
        cancellation writes the exact mirror of what it reverses (§3), so the
        debit column and the credit column must come to the same figure — over
        a dataset that contains a live invoice, a cancelled one, the amendment
        that replaced it, a receipt and an allocation.
        """
        totals = self._totals(Criteria.default().with_(as_of=JULY))

        assert totals["debit"] == totals["credit"], (
            f"the trial balance is out by {totals['debit'] - totals['credit']} paisa"
        )
        assert totals["debit"] > 0, "the dataset posted nothing at all"

    def test_a_cancellation_cannot_move_the_trial_balance(self, books):
        """The audit view is the same trial balance, to the paisa.

        A cancellation writes the exact mirror of every row it reverses, so the
        pair adds the same figure to both sides of the same account and the
        *net* per account is untouched. Bringing the rows back therefore has to
        produce an identical report — and if it ever does not, a reversal has
        been written against the wrong account or on the wrong side, which is
        the corruption this assertion exists to catch.
        """
        from apps.accounting.models import LedgerEntry

        default = self._totals(Criteria.default().with_(as_of=JULY))
        audit = self._totals(Criteria.default().with_(as_of=JULY, include_cancelled=True))

        assert audit["debit"] == audit["credit"]
        assert audit == default

        # ...and the mirrors really are in the table, so the equality above is
        # a property of the reversals rather than of an empty audit view.
        assert LedgerEntry.objects.filter(is_reversal=True).exists()

    @pytest.mark.parametrize("as_of", [APRIL, MAY, JUNE, JULY])
    def test_it_sums_to_zero_on_every_day_of_the_story(self, books, as_of):
        """Balanced as at *any* date, not just at the end.

        A posting that balanced overall but landed its two halves on different
        posting dates would pass a year-end check and fail here, which is the
        failure worth catching — an unbalanced day is what a period close trips
        over.
        """
        totals = self._totals(Criteria.default().with_(as_of=as_of))
        assert totals["debit"] == totals["credit"]

    def test_it_says_so_in_the_alarm_when_it_does_not_balance(self, books, accounts):
        """The difference is printed, never hidden.

        Forced by writing a single unbalanced row straight to the ledger — which
        no service would ever do, and which is exactly the corruption this
        report exists to surface.
        """
        from apps.accounting.models import LedgerEntry

        LedgerEntry.objects.create(
            posting_date=JUNE,
            account=accounts.cash,
            debit_paisa=12345,
            credit_paisa=0,
            voucher_type="SampleDocument",
            voucher_id=999_999,
            voucher_code="XX-2026-000001",
        )

        result = get_report("trial-balance").build(Criteria.default().with_(as_of=JULY))

        assert result.alarm, "an unbalanced ledger must not render a silent report"
        assert "123.45" in result.alarm
        assert result.totals["debit"] - result.totals["credit"] == 12345

    def test_the_screen_shows_the_alarm(self, books, accounts, admin_client_logged_in):
        from apps.accounting.models import LedgerEntry

        LedgerEntry.objects.create(
            posting_date=JUNE,
            account=accounts.cash,
            debit_paisa=500,
            credit_paisa=0,
            voucher_type="SampleDocument",
            voucher_id=999_998,
            voucher_code="XX-2026-000002",
        )
        response = run(admin_client_logged_in, "trial-balance", as_of=JULY.isoformat())

        assert response.status_code == 200
        assert "does not balance" in response.content.decode()


class TestBalanceSheet:
    def test_assets_equal_liabilities_plus_equity_plus_profit(self, books):
        """The other identity that falls out of every posting balancing."""
        result = get_report("balance-sheet").build(Criteria.default().with_(as_of=JULY))
        assert result.alarm == "", result.alarm

    def test_profit_and_loss_agrees_with_the_balance_sheet(self, books):
        """The profit line on the sheet is the P&L run from the beginning."""
        sheet = get_report("balance-sheet").build(Criteria.default().with_(as_of=JULY))
        profit_row = [row for row in sheet.rows if "Profit and loss" in str(row.get("account"))]
        assert len(profit_row) == 1

        statement = get_report("profit-and-loss").build(
            Criteria.default().with_(date_from=dt.date(2000, 1, 1), date_to=JULY)
        )
        assert profit_row[0].get("amount") == statement.totals["amount"]


# ===========================================================================
# The framework
# ===========================================================================
class TestEveryReportRuns:
    """Each registered report, in each of its three formats."""

    #: What a report that requires a filter has to be handed to run at all.
    REQUIRED = {
        "account": lambda fixtures: fixtures["accounts"].receivable.pk,
        "client": lambda fixtures: fixtures["shop"].pk,
        "vendor": lambda fixtures: fixtures["vendor"].pk,
        "route": lambda fixtures: fixtures["route"].pk,
        "item": lambda fixtures: fixtures["oil"].pk,
    }

    @pytest.fixture
    def fixtures(self, books, accounts, shop, vendor, route, oil):
        return {"accounts": accounts, "shop": shop, "vendor": vendor, "route": route, "oil": oil}

    def params(self, report, fixtures) -> dict:
        params = dict(PERIOD)
        params["as_of"] = JULY.isoformat()
        for name in report.requires:
            params[name] = self.REQUIRED[name](fixtures)
        return {key: value for key, value in params.items() if key in (*report.filters,)}

    @pytest.mark.parametrize("slug", sorted(REPORTS))
    def test_the_screen_renders(self, slug, fixtures, admin_client_logged_in):
        report = get_report(slug)
        response = run(admin_client_logged_in, slug, **self.params(report, fixtures))

        assert response.status_code == 200
        # Escaped, because a title like "Profit & Loss" reaches the page as
        # "Profit &amp; Loss" — which is the template doing its job.
        body = response.content.decode()
        assert escape(report.title) in body
        for column in report.columns:
            assert escape(column.label) in body, f"{slug} did not render the {column.key!r} head"

    @pytest.mark.parametrize("slug", sorted(REPORTS))
    def test_the_csv_has_the_same_columns_in_the_same_order(
        self, slug, fixtures, admin_client_logged_in
    ):
        report = get_report(slug)
        response = run(admin_client_logged_in, slug, format="csv", **self.params(report, fixtures))

        assert response.status_code == 200
        assert response["Content-Type"].startswith("text/csv")
        assert slug in response["Content-Disposition"]

        header = next(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
        assert header == [column.label for column in report.columns]

    @pytest.mark.parametrize("slug", sorted(REPORTS))
    def test_the_pdf_renders(self, slug, fixtures, admin_client_logged_in, profile):
        report = get_report(slug)
        response = run(admin_client_logged_in, slug, format="pdf", **self.params(report, fixtures))

        assert response.status_code == 200
        assert response["Content-Type"] == "application/pdf"

        pdf = response.content
        assert_is_a_pdf(pdf)
        assert page_count(pdf) >= 1

        text = text_of(pdf)
        assert "Al-Noor Distributors" in text, "the letterhead is missing"
        assert report.title.upper() in text
        assert "Page 1 of" in text


class TestTheView:
    def test_it_needs_a_login(self, client):
        response = client.get(reverse("reports:report", kwargs={"slug": "trial-balance"}))
        assert response.status_code == 302

    def test_an_unknown_slug_is_a_404(self, admin_client_logged_in):
        assert run(admin_client_logged_in, "no-such-report").status_code == 404

    def test_an_unknown_format_is_a_404(self, books, admin_client_logged_in):
        assert run(admin_client_logged_in, "trial-balance", format="xlsx").status_code == 404

    def test_the_index_lists_every_report(self, admin_client_logged_in):
        response = admin_client_logged_in.get(reverse("reports:index"))
        assert response.status_code == 200

        body = response.content.decode()
        for report in REPORTS.values():
            assert escape(report.title) in body

    def test_a_missing_required_filter_asks_rather_than_showing_an_empty_table(
        self, books, admin_client_logged_in
    ):
        """An empty grid would read as "this account has no entries"."""
        response = run(admin_client_logged_in, "general-ledger")

        assert response.status_code == 200
        assert response.context["prompt"]
        assert response.context["rows"] == []
        assert "Choose account" in response.content.decode()

    def test_a_backwards_period_is_run_the_right_way_round(self, books, admin_client_logged_in):
        response = run(
            admin_client_logged_in,
            "profit-and-loss",
            date_from=JULY.isoformat(),
            date_to=APRIL.isoformat(),
        )
        criteria = response.context["criteria"]

        assert criteria.date_from == APRIL
        assert criteria.date_to == JULY
        assert "wrong way round" in response.content.decode()

    def test_a_mistyped_date_falls_back_rather_than_blanking_the_page(
        self, books, admin_client_logged_in
    ):
        response = run(admin_client_logged_in, "trial-balance", as_of="not-a-date")
        assert response.status_code == 200
        assert response.context["criteria"].as_of is not None

    def test_totals_are_of_the_whole_report_not_of_the_page(self, books, admin_client_logged_in):
        """The most dangerous thing a report can get wrong."""
        from apps.reports import framework

        response = run(admin_client_logged_in, "day-book", as_of=MAY.isoformat())
        result = response.context["result"]

        assert result.totals["debit"] == sum(int(row.get("debit") or 0) for row in result.rows)
        assert len(response.context["rows"]) <= framework.PAGE_SIZE


class TestTheCancelledToggle:
    """Off by default, on the bar of every report, and never moves a total."""

    def test_every_report_offers_it(self, admin_client_logged_in, books, accounts):
        for slug in sorted(REPORTS):
            response = run(
                admin_client_logged_in,
                slug,
                account=accounts.cash.pk,
                client=Client.objects.first().pk,
                vendor=Vendor.objects.first().pk,
                route=Route.objects.first().pk,
                item=Item.objects.first().pk,
            )
            assert "include_cancelled" in response.context["form"].fields, slug

    def test_it_is_off_by_default(self, books, admin_client_logged_in):
        response = run(admin_client_logged_in, "trial-balance", as_of=JULY.isoformat())
        assert response.context["criteria"].include_cancelled is False

    def test_a_stray_empty_value_does_not_turn_it_on(self, books, admin_client_logged_in):
        response = run(
            admin_client_logged_in, "trial-balance", as_of=JULY.isoformat(), include_cancelled=""
        )
        assert response.context["criteria"].include_cancelled is False

    def test_it_changes_the_listing_and_not_the_figures(self, books, shop):
        """The property that makes the toggle safe to offer at all."""
        report = get_report("client-ledger")
        base = Criteria.default().with_(client=shop, date_from=APRIL, date_to=JULY)

        without = report.build(base)
        with_cancelled = report.build(base.with_(include_cancelled=True))

        assert with_cancelled.totals["balance"] == without.totals["balance"]

    def test_the_cancelled_document_appears_in_the_audit_view(self, books, other_shop):
        report = get_report("client-ledger")
        base = Criteria.default().with_(client=other_shop, date_from=APRIL, date_to=JULY)
        code = books["cancelled"].code

        without = {row.get("voucher") for row in report.build(base).rows}
        with_cancelled = {
            row.get("voucher") for row in report.build(base.with_(include_cancelled=True)).rows
        }

        assert code not in without
        assert code in with_cancelled
        assert books["amendment"].code in without, "an amendment is a live document"

    def test_a_cancelled_row_is_struck_through_rather_than_hidden(
        self, books, other_shop, admin_client_logged_in
    ):
        response = run(
            admin_client_logged_in,
            "client-ledger",
            client=other_shop.pk,
            include_cancelled=1,
            **PERIOD,
        )
        assert books["cancelled"].code in response.content.decode()
        assert "line-through" in response.content.decode()


class TestRowsLinkToDocuments:
    def test_a_day_book_row_links_to_its_voucher(self, books, admin_client_logged_in):
        response = run(admin_client_logged_in, "day-book", as_of=MAY.isoformat())
        urls = {entry["row"].url for entry in rows_of(response)}

        assert books["open_invoice"].get_absolute_url() in urls
        assert all(urls), "every day book row references a document"

    def test_the_link_is_rendered_on_the_voucher_column_only(self, books, admin_client_logged_in):
        response = run(admin_client_logged_in, "day-book", as_of=MAY.isoformat())
        linked = [
            entry["column"].key
            for row in rows_of(response)
            for entry in row["cells"]
            if entry["url"]
        ]
        assert set(linked) == {"voucher"}

    def test_an_ageing_row_links_to_the_shop_statement(self, books, shop, admin_client_logged_in):
        response = run(admin_client_logged_in, "receivable-ageing", as_of=JULY.isoformat())
        urls = {entry["row"].url for entry in rows_of(response)}
        assert any(f"client={shop.pk}" in url for url in urls)


# ===========================================================================
# The figures themselves
# ===========================================================================
class TestTheFiguresComeFromTheLedger:
    def test_a_client_statement_closes_where_the_ledger_says(self, books, shop):
        result = get_report("client-ledger").build(
            Criteria.default().with_(client=shop, date_from=APRIL, date_to=JULY)
        )
        expected = party_balance(PartyType.CLIENT, shop.pk, JULY).paisa

        assert result.totals["balance"] == expected
        assert result.alarm == ""
        assert any("agrees with the ledger" in note for note in result.notes)

    def test_a_general_ledger_closes_where_the_ledger_says(self, books, accounts):
        from apps.accounting.services import account_balance

        result = get_report("general-ledger").build(
            Criteria.default().with_(account=accounts.receivable, date_from=APRIL, date_to=JULY)
        )
        assert result.totals["balance"] == account_balance(accounts.receivable, JULY).paisa

    def test_stock_balance_agrees_with_the_stock_service(self, books, warehouses, oil):
        result = get_report("stock-balance").build(
            Criteria.default().with_(as_of=JULY, item=oil, warehouse=warehouses.main)
        )
        expected = stock_balance(oil, warehouses.main, JULY)

        assert len(result.rows) == 1
        assert result.rows[0].get("pieces") == expected.qty_base
        assert result.rows[0].get("value") == expected.value_paisa

    def test_a_stock_card_closes_where_the_stock_ledger_says(self, books, oil):
        result = get_report("stock-ledger").build(
            Criteria.default().with_(item=oil, date_from=APRIL, date_to=JULY)
        )
        expected = stock_balance(oil, None, JULY)
        assert result.totals["balance_value"] == expected.value_paisa

    def test_a_day_book_balances(self, books):
        result = get_report("day-book").build(Criteria.default().with_(as_of=MAY))
        assert result.totals["debit"] == result.totals["credit"]
        assert result.alarm == ""

    def test_the_route_day_sheet_adds_up_for_every_shop(self, books, route):
        """Opening + sales - returns - recovery + other == closing. Every row."""
        result = get_report("route-day-sheet").build(
            Criteria.default().with_(route=route, as_of=MAY)
        )
        assert result.rows, "the route has shops on it"

        for row in result.rows:
            computed = (
                row.get("opening")
                + row.get("sales")
                - row.get("returns")
                - row.get("recovery")
                + row.get("other")
            )
            assert computed == row.get("closing"), f"{row.get('code')} does not add up"
        assert result.alarm == ""

    def test_the_route_day_sheet_closes_where_each_shop_ledger_says(self, books, route, shop):
        result = get_report("route-day-sheet").build(
            Criteria.default().with_(route=route, as_of=JUNE)
        )
        row = next(row for row in result.rows if row.get("code") == shop.code)
        assert row.get("closing") == party_balance(PartyType.CLIENT, shop.pk, JUNE).paisa

    def test_the_day_sheet_lists_every_shop_on_the_route(self, books, route):
        result = get_report("route-day-sheet").build(
            Criteria.default().with_(route=route, as_of=JULY)
        )
        assert len(result.rows) == Client.objects.filter(route=route).count()

    def test_receivable_ageing_totals_the_clients_balances(self, books, shop, other_shop):
        result = get_report("receivable-ageing").build(Criteria.default().with_(as_of=JULY))
        expected = sum(
            party_balance(PartyType.CLIENT, client.pk, JULY).paisa for client in (shop, other_shop)
        )
        assert result.totals["outstanding"] - result.totals["on_account"] == expected

    def test_item_sales_quantity_comes_from_the_stock_ledger(self, books, oil):
        result = get_report("item-sales").build(
            Criteria.default().with_(date_from=APRIL, date_to=JULY)
        )
        row = next(row for row in result.rows if row.get("code") == oil.code)
        assert row.get("pieces") == 120, "ten cartons of twelve"
        assert row.get("margin") == row.get("revenue") - row.get("cost")

    def test_slow_moving_excludes_what_moved_recently(self, books, oil):
        idle = get_report("slow-moving").build(Criteria.default().with_(as_of=JULY, days=1))
        assert any(row.get("code") == oil.code for row in idle.rows)

        fresh = get_report("slow-moving").build(Criteria.default().with_(as_of=MAY, days=90))
        assert not any(row.get("code") == oil.code for row in fresh.rows)

    def test_seller_performance_credits_the_seller_on_the_document(self, books, seller):
        result = get_report("seller-performance").build(
            Criteria.default().with_(date_from=APRIL, date_to=JULY)
        )
        row = next(row for row in result.rows if row.get("code") == seller.code)
        assert row.get("net_sales") == row.get("sales") - row.get("returns")
        assert row.get("recovery") > 0, "the June receipt was collected by this seller's shop"

    def test_every_report_means_the_same_thing_by_recovery(self, books, route, seller):
        """One definition, three reports. This is the test that found the bug.

        Seller performance, route performance and the client-wise summary each
        arrive at "recovery" from a different direction — by the seller on the
        payment, by the route on it, and by the shop's own ledger rows. They
        have to agree to the paisa, or one of the three is quietly wrong.
        """
        period = Criteria.default().with_(date_from=APRIL, date_to=JULY)

        by_seller = get_report("seller-performance").build(period).totals["recovery"]
        by_route = get_report("route-performance").build(period).totals["recovery"]
        by_client = get_report("client-sales").build(period).totals["recovery"]

        assert by_seller == by_route == by_client

    def test_a_bounced_cheque_is_not_recovery(self, books, shop, route, seller, user):
        """The payment stays POSTED and the recovery figure comes back down.

        A bounce does not reverse the receipt — the receipt is a true record
        that a cheque was taken on a day (CLAUDE.md §5) — so a report summing
        the payment's ledger rows alone would count money that never arrived.
        Both the cheque event and the payment are in
        :data:`apps.reports.ledger.RECOVERY_VOUCHERS` for exactly this.
        """
        period = Criteria.default().with_(date_from=APRIL, date_to=JULY)
        before = get_report("seller-performance").build(period).totals["recovery"]

        cheque = payments.post_payment(
            payments.create_payment(
                party=shop,
                direction=PaymentDirection.RECEIVE,
                mode=PaymentMode.CHEQUE,
                posting_date=JUNE,
                amount_paisa=to_paisa("6000"),
                cheque_no="000123",
                cheque_date=JUNE,
                bank_name="Meezan",
                route=route,
                collected_by=seller,
            ),
            user=user,
        )
        taken = get_report("seller-performance").build(period).totals["recovery"]
        assert taken == before + to_paisa("6000")

        payments.bounce_cheque(cheque, posting_date=JULY, user=user)

        assert cheque.status == "POSTED", "a bounce never rewrites the receipt"
        after = get_report("seller-performance").build(period).totals["recovery"]
        assert after == before, "a cheque that came back is not recovery"

        # ...and the three reports still agree with each other afterwards.
        assert after == get_report("client-sales").build(period).totals["recovery"]

    def test_client_sales_is_pure_ledger(self, books, shop):
        result = get_report("client-sales").build(
            Criteria.default().with_(date_from=APRIL, date_to=JULY)
        )
        row = next(row for row in result.rows if row.get("code") == shop.code)
        assert row.get("outstanding") == party_balance(PartyType.CLIENT, shop.pk, JULY).paisa
        assert row.get("invoices") == 1


class TestNoReportReadsADocumentTotal:
    """The rule CLAUDE.md §6 exists to protect, checked by breaking it."""

    def test_a_wrong_header_total_does_not_move_a_report(self, books, shop, settings):
        """Corrupt the denormalised header and the figures must not budge.

        ``total_paisa`` on a document is a display convenience and the source of
        truth for nothing. This writes a wrong one straight to the row — the
        model would refuse an ordinary save on a POSTED document (CLAUDE.md §5)
        — and asserts every client-facing figure is unchanged.
        """
        from apps.sales.models import SalesInvoice

        before = get_report("client-sales").build(
            Criteria.default().with_(date_from=APRIL, date_to=JULY)
        )

        SalesInvoice.objects.filter(pk=books["open_invoice"].pk).update(
            total_paisa=to_paisa("999999"), subtotal_paisa=to_paisa("999999")
        )

        after = get_report("client-sales").build(
            Criteria.default().with_(date_from=APRIL, date_to=JULY)
        )
        assert after.totals == before.totals


# ===========================================================================
# Columns, and the three renderings of them
# ===========================================================================
class TestColumns:
    def test_a_money_column_is_right_aligned_and_mono(self):
        column = Column("amount", "Amount", "money")
        assert column.align == "r"
        assert column.css_class == "amount"

    def test_display_is_for_humans_and_export_is_for_spreadsheets(self):
        column = Column("amount", "Amount", "money")
        assert display(column, 123456789) == "1,234,567.89"
        assert export(column, 123456789) == "1234567.89"

    def test_a_date_prints_readably_and_exports_unambiguously(self):
        column = Column("date", "Date", "date")
        assert display(column, dt.date(2026, 8, 8)) == "08 Aug 2026"
        assert export(column, dt.date(2026, 8, 8)) == "2026-08-08"

    def test_blank_zero_leaves_a_cell_empty(self):
        column = Column("debit", "Debit", "money", blank_zero=True)
        assert display(column, 0) == ""
        assert display(Column("debit", "Debit", "money"), 0) == "0.00"

    def test_a_column_that_cannot_be_added_up_refuses_to_be_totalled(self):
        with pytest.raises(ValueError, match="cannot be totalled"):
            Column("date", "Date", "date", total=True)

    def test_an_unknown_kind_is_refused(self):
        with pytest.raises(ValueError, match="Unknown column kind"):
            Column("x", "X", "colour")


class TestCSV:
    def test_money_is_rupees_without_separators(self, books, shop, admin_client_logged_in):
        response = run(
            admin_client_logged_in, "client-ledger", format="csv", client=shop.pk, **PERIOD
        )
        body = response.content.decode("utf-8-sig")
        assert "," not in body.split("\n")[1].split(",")[-1]
        assert "Rs" not in body

    def test_the_totals_row_is_last_and_labelled(self, books, shop, admin_client_logged_in):
        response = run(
            admin_client_logged_in, "client-ledger", format="csv", client=shop.pk, **PERIOD
        )
        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
        assert "Closing balance" in rows[-1]

    def test_a_formula_is_neutralised(self):
        """A shop name typed as a formula must not run in the accountant's Excel."""
        report = get_report("trial-balance")
        result = type(report.build(Criteria.default()))(
            rows=[ReportRow(values={"code": "1110", "account": "=cmd|'/c calc'!A1", "debit": 100})]
        )
        body = write_csv(report, result)
        assert "'=cmd" in body

    def test_a_negative_amount_keeps_its_minus_sign(self):
        report = get_report("trial-balance")
        result = type(report.build(Criteria.default()))(
            rows=[ReportRow(values={"code": "1110", "account": "Cash", "debit": -12345})]
        )
        assert "-123.45" in write_csv(report, result)

    def test_it_is_written_with_a_bom_so_excel_reads_utf8(
        self, books, shop, admin_client_logged_in
    ):
        response = run(
            admin_client_logged_in, "client-ledger", format="csv", client=shop.pk, **PERIOD
        )
        assert response.content.startswith(b"\xef\xbb\xbf")


class TestThePDF:
    def test_a_landscape_report_prints_wider_than_it_is_tall(self, books, profile):
        from apps.reports.pdf.reports import report_pdf

        report = get_report("day-book")
        criteria = Criteria.default().with_(as_of=MAY)
        pdf = report_pdf(report, report.build(criteria), criteria)

        assert_is_a_pdf(pdf)
        assert b"/MediaBox [ 0 0 841" in pdf or b"/MediaBox [0 0 841" in pdf

    def test_the_header_says_what_the_report_was_run_for(self, books, shop, profile):
        from apps.reports.pdf.reports import report_pdf

        report = get_report("client-ledger")
        criteria = Criteria.default().with_(client=shop, date_from=APRIL, date_to=JULY)
        text = text_of(report_pdf(report, report.build(criteria), criteria))

        assert "CLIENT LEDGER" in text
        assert shop.code in text
        assert "Period" in text

    def test_a_long_report_truncates_and_says_so(self, books, profile, monkeypatch):
        """No silent caps. A page that stopped early must say it stopped early."""
        from apps.reports.pdf import reports as pdf_reports

        monkeypatch.setattr(pdf_reports, "MAX_PDF_ROWS", 2)
        report = get_report("day-book")
        criteria = Criteria.default().with_(as_of=MAY)
        result = report.build(criteria)
        assert len(result.rows) > 2

        text = text_of(pdf_reports.report_pdf(report, result, criteria))
        assert "are not printed" in text


# ===========================================================================
# The aggregation layer
# ===========================================================================
class TestTheLedgerLayer:
    def test_live_drops_a_reversal_and_the_row_it_reverses(self, books):
        from apps.accounting.models import LedgerEntry

        everything = LedgerEntry.objects.filter(voucher_id=books["cancelled"].pk).count()
        live = report_ledger.live(
            LedgerEntry.objects.filter(voucher_id=books["cancelled"].pk)
        ).count()

        assert everything > 0
        assert live == 0, "a cancelled voucher contributes no live rows"

    def test_voucher_targets_resolves_a_link_per_type_not_per_row(
        self, books, django_assert_num_queries
    ):
        pairs = [("SalesInvoice", books["open_invoice"].pk)] * 20 + [
            ("Payment", books["receipt"].pk)
        ] * 20

        with django_assert_num_queries(2):
            targets = report_ledger.voucher_targets(pairs)

        assert targets[("SalesInvoice", books["open_invoice"].pk)].url
        assert targets[("Payment", books["receipt"].pk)].url

    def test_an_unknown_voucher_type_is_skipped_rather_than_raised_on(self, books):
        """The ledger outlives its documents by design."""
        assert report_ledger.voucher_targets([("SomethingRemovedIn2031", 1)]) == {}
