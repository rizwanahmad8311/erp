"""The dashboard: whose figures, from where, and cached for how long.

Four properties, and the first two are the ones that would be expensive to get
wrong.

* **Every figure is the ledger's.** The cards are asserted against
  :func:`apps.accounting.services.party_balance` and
  :func:`~apps.accounting.services.account_balance` rather than against the
  numbers the fixtures typed in — so a dashboard that started reading
  ``SalesInvoice.total_paisa`` would fail here the first time a document was
  cancelled (CLAUDE.md §6).
* **Role decides what is on the page, not what is styled away.** An Operator's
  dashboard must carry no rupee figure at all, and that is asserted against the
  built object *and* against the rendered HTML — a figure hidden by a template
  is a figure still in the response.
* **A booker sees their own beats.** Another route's invoice is not in their
  sales, their receivable or their overdue list.
* **The cache is per user and expires.** Two logins must never share an entry.

The chart is checked the way ``tests/test_pdf.py`` checks a PDF: it has the
points it claims and the numbers are the ledger's. Nothing pins a coordinate.
"""

from __future__ import annotations

import datetime as dt

import pytest
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone

from apps.accounting.enums import PartyType
from apps.accounting.services import account_balance, party_balance, stock_balance
from apps.accounts.models import UserProfile
from apps.core.money import to_paisa
from apps.masters.enums import DayOfWeek, Unit
from apps.masters.models import Client, Item, Route, RouteSeller, Seller, Vendor
from apps.payments import services as payments
from apps.payments.enums import PaymentDirection, PaymentMode
from apps.purchasing import services as purchasing
from apps.purchasing.models import PurchaseInvoiceLine
from apps.reports import dashboard as board
from apps.sales import services as sales
from apps.sales.models import SalesInvoiceLine
from tests.conftest import ensure_groups, join_group

pytestmark = pytest.mark.django_db

#: Everything below is measured against this day. Fixed, not "today", because a
#: suite that drifts with the calendar fails on one particular Tuesday a year.
DAY = dt.date(2026, 6, 30)

#: The day of the week DAY falls on, so the "routes running today" panel has
#: something to find without the fixture hard-coding a weekday that would be
#: wrong the moment DAY moved.
DAY_OF_WEEK = DayOfWeek.values[DAY.weekday()]

BIG_LIMIT = to_paisa("10000000")


# ===========================================================================
# A small business on two beats
# ===========================================================================
@pytest.fixture
def routes(db):
    from types import SimpleNamespace

    return SimpleNamespace(
        ours=Route.objects.create(code="R-01", name="Saddar & City", day_of_week=DAY_OF_WEEK),
        theirs=Route.objects.create(code="R-02", name="North Nazimabad", day_of_week=DAY_OF_WEEK),
    )


@pytest.fixture
def sellers(db, routes):
    from types import SimpleNamespace

    ours = Seller.objects.create(code="S-01", name="Imran Qureshi")
    theirs = Seller.objects.create(code="S-02", name="Bilal Ahmed")
    RouteSeller.objects.create(route=routes.ours, seller=ours, is_primary=True)
    RouteSeller.objects.create(route=routes.theirs, seller=theirs, is_primary=True)
    return SimpleNamespace(ours=ours, theirs=theirs)


@pytest.fixture
def shops(db, routes, sellers):
    from types import SimpleNamespace

    return SimpleNamespace(
        ours=Client.objects.create(
            code="C-0001",
            name="Al-Madina Kiryana",
            phone="0300-221 4477",
            route=routes.ours,
            seller=sellers.ours,
            credit_limit_paisa=BIG_LIMIT,
            credit_days=0,
        ),
        theirs=Client.objects.create(
            code="C-0002",
            name="Nazimabad General Store",
            phone="",
            route=routes.theirs,
            seller=sellers.theirs,
            credit_limit_paisa=BIG_LIMIT,
            credit_days=0,
        ),
    )


@pytest.fixture
def oil(db):
    """One item, watched: a reorder level is what puts it on the low-stock panel."""
    return Item.objects.create(
        code="OIL-1000", name="Cooking Oil 1L", carton_size=12, reorder_level_pieces=600
    )


@pytest.fixture
def stocked(db, accounts, warehouses, oil, user):
    """Plenty of stock, received long before anything below is dated.

    Back-dated on purpose: an issue is valued against the position as it stood
    on its posting date, so goods received today cannot be sold last year.
    """
    vendor = Vendor.objects.create(code="V-01", name="Dalda Foods")
    bill = purchasing.create_purchase_invoice(
        vendor=vendor, warehouse=warehouses.main, posting_date=dt.date(2024, 1, 1)
    )
    purchasing.update_line(
        PurchaseInvoiceLine(document=bill),
        item=oil,
        qty_input=100,
        unit_input=Unit.CARTON,
        rate_input_paisa=to_paisa("2400"),
    ).save()
    return purchasing.post_purchase_invoice(bill, user=user)


# ---------------------------------------------------------------------------
# The two things that happen in a day
# ---------------------------------------------------------------------------
def bill(shop, warehouse, item, user, *, on=DAY, rupees="1000", cartons=1, due_date=None):
    """A posted sales invoice for a round number of rupees."""
    invoice = sales.create_sales_invoice(
        client=shop, warehouse=warehouse, posting_date=on, due_date=due_date
    )
    sales.update_line(
        SalesInvoiceLine(document=invoice),
        item=item,
        qty_input=cartons,
        unit_input=Unit.CARTON,
        rate_input_paisa=to_paisa(rupees),
    ).save()
    return sales.post_sales_invoice(invoice, user=user)


def take(shop, *, rupees, on=DAY, mode=PaymentMode.CASH, user=None, **fields):
    """A posted receipt. The route defaults from the shop, as it does on screen."""
    payment = payments.create_payment(
        party=shop,
        direction=PaymentDirection.RECEIVE,
        mode=mode,
        posting_date=on,
        amount_paisa=to_paisa(rupees),
        **fields,
    )
    return payments.post_payment(payment, user=user)


# ---------------------------------------------------------------------------
# One login per role
# ---------------------------------------------------------------------------
def make_user(django_user_model, username: str, group: str | None = None, seller=None):
    user = django_user_model.objects.create_user(username=username, password="x", is_staff=True)
    if group:
        join_group(user, group)
    if seller is not None:
        profile = UserProfile.for_user(user)
        profile.seller = seller
        profile.save(update_fields=["seller", "updated_at"])
    return user


@pytest.fixture
def people(django_user_model, sellers, db):
    """One login per group. The Booker walks R-01 and nothing else."""
    ensure_groups()
    return {
        "Admin": make_user(django_user_model, "board-admin", "Admin"),
        "Accountant": make_user(django_user_model, "board-accountant", "Accountant"),
        "Operator": make_user(django_user_model, "board-operator", "Operator"),
        "Booker": make_user(django_user_model, "board-booker", "Booker", seller=sellers.ours),
        "Viewer": make_user(django_user_model, "board-viewer", "Viewer"),
    }


@pytest.fixture
def trading(db, stocked, shops, warehouses, oil, user):
    """A day's trading on both beats: two bills and one receipt."""
    from types import SimpleNamespace

    return SimpleNamespace(
        ours=bill(shops.ours, warehouses.main, oil, user, rupees="1000", cartons=2),
        theirs=bill(shops.theirs, warehouses.main, oil, user, rupees="1000", cartons=3),
        receipt=take(shops.ours, rupees="500", user=user),
    )


def card(built, key: str):
    """One card off the top row, or ``None`` when this login is not shown it."""
    return next((entry for entry in built.cards if entry.key == key), None)


# ===========================================================================
# Where the figures come from
# ===========================================================================
class TestTheFiguresAreTheLedgers:
    """Asserted against the ledger, never against what the fixture typed in.

    This is the test that fails if the dashboard ever starts reading a document
    header (CLAUDE.md §6).
    """

    def test_sales_today_is_what_was_billed_today(self, people, trading):
        """Two carton bills at Rs 1,000 a carton: two on one beat, three on the other."""
        built = board.build(people["Accountant"], day=DAY)
        assert card(built, "sales").paisa == to_paisa("5000")

    def test_billed_less_collected_is_every_shops_ledger_balance(self, people, trading, shops):
        """The invariant, on a business whose whole history is this one day.

        Whatever the cards are computed from, ``sales - recovery`` has to be the
        movement the ledger actually recorded — which on a book that opened this
        morning is exactly what every shop now owes.
        """
        built = board.build(people["Accountant"], day=DAY)
        owed = sum(
            party_balance(PartyType.CLIENT, shop.pk, DAY).paisa
            for shop in (shops.ours, shops.theirs)
        )
        assert card(built, "sales").paisa - card(built, "recovery").paisa == owed

    def test_recovery_today_is_what_the_ledger_says_came_in(self, people, trading):
        built = board.build(people["Accountant"], day=DAY)
        assert card(built, "recovery").paisa == to_paisa("500")

    def test_a_cancelled_invoice_leaves_no_trace_in_the_figures(
        self, people, trading, shops, warehouses, oil, user
    ):
        """The property a header total could not have.

        The invoice and its reversal are both in the ledger and net to zero, so
        the figure is right without anything here having to know it happened.
        """
        before = card(board.build(people["Accountant"], day=DAY), "sales").paisa

        extra = bill(shops.ours, warehouses.main, oil, user, rupees="700", cartons=1)
        sales.cancel_sales_invoice(extra, user=user, reason="Keyed against the wrong shop.")

        after = card(board.build(people["Accountant"], day=DAY), "sales").paisa
        assert after == before

    def test_cash_and_bank_are_account_balances(self, accounts, people, trading):
        built = board.build(people["Accountant"], day=DAY)
        expected = (
            account_balance(accounts.cash, DAY).paisa + account_balance(accounts.bank, DAY).paisa
        )
        assert card(built, "cash").paisa == expected

    def test_cheques_in_hand_is_the_account_not_a_sum_of_receipts(
        self, accounts, people, stocked, shops, user
    ):
        """A cheque in the drawer is 1160's balance, not what a document says."""
        take(
            shops.ours,
            rupees="4000",
            mode=PaymentMode.CHEQUE,
            user=user,
            cheque_no="112233",
            cheque_date=DAY + dt.timedelta(days=3),
        )

        built = board.build(people["Accountant"], day=DAY)
        cheques = card(built, "cheques")

        assert cheques.paisa == account_balance(accounts.by_code["1160"], DAY).paisa
        assert cheques.count == 1
        # Dated inside the week, so it is bankable before the next one starts.
        assert cheques.sub_count == 1

    def test_a_cheque_dated_next_month_is_held_but_not_bankable_this_week(
        self, people, stocked, shops, user
    ):
        take(
            shops.ours,
            rupees="4000",
            mode=PaymentMode.CHEQUE,
            user=user,
            cheque_no="998877",
            cheque_date=DAY + dt.timedelta(days=40),
        )

        cheques = card(board.build(people["Accountant"], day=DAY), "cheques")
        assert cheques.count == 1
        assert cheques.sub_count == 0


class TestOutstandingAndOverdue:
    """The receivable card, and the chase list under it."""

    def test_outstanding_ties_to_the_client_balances(self, people, trading, shops):
        built = board.build(people["Accountant"], day=DAY)
        expected = sum(
            max(party_balance(PartyType.CLIENT, shop.pk, DAY).paisa, 0)
            for shop in (shops.ours, shops.theirs)
        )
        assert card(built, "receivable").paisa == expected

    def test_sixty_plus_counts_only_the_two_oldest_bands(
        self, people, stocked, shops, warehouses, oil, user
    ):
        """A bill 70 days past due is in; one 40 days past due is not."""
        bill(
            shops.ours,
            warehouses.main,
            oil,
            user,
            rupees="900",
            on=DAY - dt.timedelta(days=70),
            due_date=DAY - dt.timedelta(days=70),
        )
        bill(
            shops.theirs,
            warehouses.main,
            oil,
            user,
            rupees="400",
            on=DAY - dt.timedelta(days=40),
            due_date=DAY - dt.timedelta(days=40),
        )

        receivable = card(board.build(people["Accountant"], day=DAY), "receivable")
        assert receivable.sub_paisa == to_paisa("900")
        assert receivable.sub_alarm is True

    def test_the_bands_are_derived_from_the_ladder(self):
        """Not typed out — a band moved in AGEING_LADDER moves this figure."""
        assert board.SIXTY_PLUS_BUCKETS == ("61-90", "90+")

    def test_the_chase_list_is_ranked_by_what_is_overdue(
        self, people, stocked, shops, warehouses, oil, user
    ):
        """Not by what is owed. A large bill that is not due yet is a good customer."""
        bill(
            shops.theirs,
            warehouses.main,
            oil,
            user,
            rupees="5000",
            cartons=1,
            on=DAY,
            due_date=DAY + dt.timedelta(days=30),
        )
        bill(
            shops.ours,
            warehouses.main,
            oil,
            user,
            rupees="800",
            cartons=1,
            on=DAY - dt.timedelta(days=80),
            due_date=DAY - dt.timedelta(days=80),
        )

        overdue = board.build(people["Accountant"], day=DAY).overdue
        assert [row.code for row in overdue] == ["C-0001"]
        assert overdue[0].overdue_paisa == to_paisa("800")

    def test_the_phone_number_is_dialable_and_the_written_one_is_shown(
        self, people, stocked, shops, warehouses, oil, user
    ):
        bill(
            shops.ours,
            warehouses.main,
            oil,
            user,
            rupees="800",
            on=DAY - dt.timedelta(days=80),
            due_date=DAY - dt.timedelta(days=80),
        )

        row = board.build(people["Accountant"], day=DAY).overdue[0]
        assert row.phone == "0300-221 4477"
        assert row.tel == "tel:03002214477"
        assert row.has_phone is True

    def test_a_shop_with_no_number_gets_no_link(
        self, people, stocked, shops, warehouses, oil, user
    ):
        bill(
            shops.theirs,
            warehouses.main,
            oil,
            user,
            rupees="800",
            on=DAY - dt.timedelta(days=80),
            due_date=DAY - dt.timedelta(days=80),
        )

        row = board.build(people["Accountant"], day=DAY).overdue[0]
        assert row.tel == ""
        assert row.has_phone is False


# ===========================================================================
# Role
# ===========================================================================
class TestWhatEachRoleSees:
    """The brief, asserted: an Operator sees no financial card at all."""

    def test_an_accountant_sees_everything(self, people, trading):
        built = board.build(people["Accountant"], day=DAY)
        keys = {entry.key for entry in built.cards}
        assert keys == {"sales", "recovery", "purchases", "receivable", "cash", "cheques"}
        assert built.trend is not None
        assert built.overdue is not None

    def test_an_admin_sees_everything(self, people, trading):
        keys = {entry.key for entry in board.build(people["Admin"], day=DAY).cards}
        assert "cash" in keys and "receivable" in keys

    def test_an_operator_sees_no_financial_card(self, people, trading):
        built = board.build(people["Operator"], day=DAY)

        assert built.cards == ()
        assert built.trend is None
        assert built.overdue == ()
        assert built.shows_money is False

    def test_an_operator_still_gets_the_operational_panels(self, people, trading):
        """No money is not no page. Routes, stock and documents are still there."""
        built = board.build(people["Operator"], day=DAY)

        assert [route.code for route in built.routes] == ["R-01", "R-02"]
        assert all(route.sales_paisa is None for route in built.routes)
        assert all(route.recovery_paisa is None for route in built.routes)
        assert built.documents

    def test_a_booker_sees_money_because_they_collect_it(self, people, trading):
        built = board.build(people["Booker"], day=DAY)

        assert built.shows_money is True
        assert {entry.key for entry in built.cards} == {"sales", "recovery", "receivable"}

    def test_a_booker_is_not_shown_the_treasury(self, people, trading):
        """A bank balance has no route share, so a scoped login is shown none."""
        keys = {entry.key for entry in board.build(people["Booker"], day=DAY).cards}
        assert "cash" not in keys
        assert "cheques" not in keys

    def test_a_booker_has_no_purchases_card(self, people, trading):
        keys = {entry.key for entry in board.build(people["Booker"], day=DAY).cards}
        assert "purchases" not in keys


class TestEveryCardDrillsIntoItsOwnFigure:
    """A number nobody can drill into is a number nobody trusts."""

    def test_every_card_carries_a_link(self, people, trading):
        for name in ("Accountant", "Booker"):
            for entry in board.build(people[name], day=DAY).cards:
                assert entry.url, f"{name}'s {entry.key} card has no link"

    def test_a_bookers_card_opens_the_report_filtered_to_their_beat(self, people, routes, trading):
        """Reports are not route-scoped, so the link arrives with the filter set.

        Without this a booker's receivable card would open the whole company's
        ageing and show a different, larger number than the one they clicked.
        """
        built = board.build(people["Booker"], day=DAY)
        assert f"route={routes.ours.pk}" in card(built, "receivable").url
        assert f"route={routes.ours.pk}" in card(built, "sales").url

    def test_an_unscoped_login_gets_no_route_filter(self, people, trading):
        assert "route=" not in card(board.build(people["Accountant"], day=DAY), "receivable").url

    def test_a_booker_on_several_beats_gets_no_route_filter(
        self, django_user_model, routes, sellers, trading
    ):
        """One filter box cannot express two beats, so it opens wider rather than wrong."""
        RouteSeller.objects.create(route=routes.theirs, seller=sellers.ours)
        ensure_groups()
        walker = make_user(django_user_model, "board-walker", "Booker", seller=sellers.ours)

        assert "route=" not in card(board.build(walker, day=DAY), "receivable").url


class TestABookerSeesTheirOwnRoutes:
    """Scope, applied to every figure on the page and not only to the lists."""

    def test_sales_today_covers_their_shops_only(self, people, trading, shops):
        built = board.build(people["Booker"], day=DAY)
        expected = party_balance(PartyType.CLIENT, shops.ours.pk, DAY).paisa + to_paisa("500")
        assert card(built, "sales").paisa == expected

    def test_outstanding_covers_their_shops_only(self, people, trading, shops):
        built = board.build(people["Booker"], day=DAY)
        assert card(built, "receivable").paisa == max(
            party_balance(PartyType.CLIENT, shops.ours.pk, DAY).paisa, 0
        )

    def test_the_route_panel_is_their_beat_only(self, people, trading):
        built = board.build(people["Booker"], day=DAY)
        assert [route.code for route in built.routes] == ["R-01"]

    def test_the_chase_list_never_names_another_beats_shop(
        self, people, stocked, shops, warehouses, oil, user
    ):
        bill(
            shops.theirs,
            warehouses.main,
            oil,
            user,
            rupees="5000",
            on=DAY - dt.timedelta(days=200),
            due_date=DAY - dt.timedelta(days=200),
        )

        assert board.build(people["Booker"], day=DAY).overdue == ()

    def test_recent_documents_are_their_beat_only(self, people, trading):
        codes = {row.code for row in board.build(people["Booker"], day=DAY).documents}
        assert trading.ours.code in codes
        assert trading.theirs.code not in codes

    def test_a_scoped_login_with_no_seller_sees_nothing(self, django_user_model, sellers, trading):
        """The safe direction. Not everything — nothing."""
        ensure_groups()
        stranger = make_user(django_user_model, "board-stranger", "Booker")

        built = board.build(stranger, day=DAY)

        assert card(built, "sales").paisa == 0
        assert card(built, "receivable").paisa == 0
        assert built.routes == ()
        assert built.overdue == ()


# ===========================================================================
# The panels
# ===========================================================================
class TestTodaysRoutes:
    def test_a_route_carries_its_sellers_and_its_bills(self, people, trading):
        built = board.build(people["Accountant"], day=DAY)
        ours = next(route for route in built.routes if route.code == "R-01")

        assert ours.sellers == "Imran Qureshi"
        assert ours.invoice_count == 1
        assert ours.recovery_paisa == to_paisa("500")

    def test_a_route_that_does_not_run_today_is_not_listed(self, people, routes, trading):
        routes.theirs.day_of_week = DayOfWeek.values[(DAY.weekday() + 1) % 7]
        routes.theirs.save()

        built = board.build(people["Accountant"], day=DAY)
        assert [route.code for route in built.routes] == ["R-01"]

    def test_the_route_money_is_the_ledgers(self, people, trading, shops):
        """Not a sum over payment headers — the same split every report makes."""
        built = board.build(people["Accountant"], day=DAY)
        ours = next(route for route in built.routes if route.code == "R-01")
        assert ours.sales_paisa == to_paisa("2000")


class TestTheSalesTrend:
    def test_it_covers_thirty_days_ending_today(self, people, trading):
        trend = board.build(people["Accountant"], day=DAY).trend

        assert len(trend.points) == board.TREND_DAYS
        assert trend.points[-1].day == DAY
        assert trend.points[0].day == DAY - dt.timedelta(days=board.TREND_DAYS - 1)

    def test_a_quiet_day_is_a_point_at_zero_not_a_gap(self, people, trading):
        trend = board.build(people["Accountant"], day=DAY).trend
        yesterday = next(point for point in trend.points if point.day == DAY - dt.timedelta(days=1))
        assert yesterday.paisa == 0

    def test_the_last_point_is_todays_sales(self, people, trading):
        trend = board.build(people["Accountant"], day=DAY).trend
        assert trend.points[-1].paisa == to_paisa("5000")
        assert trend.peak_paisa == to_paisa("5000")

    def test_the_geometry_is_whole_pixels_inside_the_viewbox(self, people, trading):
        """Integers throughout, so a chart cannot be a second rounding site."""
        trend = board.build(people["Accountant"], day=DAY).trend

        for point in trend.points:
            assert isinstance(point.x, int)
            assert isinstance(point.y, int)
            assert 0 <= point.x <= trend.width
            assert 0 <= point.y <= trend.height

    def test_a_business_with_no_sales_draws_nothing_rather_than_dividing_by_zero(
        self, people, stocked
    ):
        trend = board.build(people["Accountant"], day=DAY).trend
        assert trend.peak_paisa == 0
        assert trend.has_movement is False
        assert all(point.y == trend.baseline_y for point in trend.points)


class TestLowStock:
    def test_an_item_under_its_level_is_listed_with_how_far_under(
        self, people, stocked, shops, warehouses, oil, user
    ):
        """1,200 pieces received; sell 50 cartons and 600 are left, at the level."""
        bill(shops.ours, warehouses.main, oil, user, cartons=50, rupees="3000")

        low = board.build(people["Accountant"], day=DAY).low_stock
        assert [item.code for item in low] == ["OIL-1000"]
        assert low[0].on_hand_pieces == 600
        assert low[0].reorder_level_pieces == 600
        assert low[0].short_pieces == 0

    def test_a_well_stocked_item_is_not_listed(self, people, stocked):
        assert board.build(people["Accountant"], day=DAY).low_stock == ()

    def test_an_item_nobody_watches_is_never_listed(self, people, stocked, oil):
        oil.reorder_level_pieces = 0
        oil.save()
        assert board.build(people["Accountant"], day=DAY).low_stock == ()

    def test_an_item_that_has_never_moved_is_listed_at_zero(self, people, stocked):
        """Never received is a stronger version of out of stock, not an exemption."""
        Item.objects.create(code="TEA-250", name="Tea 250g", reorder_level_pieces=40)

        low = board.build(people["Accountant"], day=DAY).low_stock
        assert [item.code for item in low] == ["TEA-250"]
        assert low[0].on_hand_pieces == 0
        assert low[0].is_out is True

    def test_the_position_is_the_stock_ledger_summed_up(
        self, people, stocked, shops, warehouses, oil, user
    ):
        """Not a field on the item — there is none, and there must not be (§6)."""
        bill(shops.ours, warehouses.main, oil, user, cartons=50, rupees="3000")

        low = board.build(people["Accountant"], day=DAY).low_stock[0]
        assert low.on_hand_pieces == stock_balance(oil, as_of=DAY).qty_base


class TestRecentDocuments:
    def test_a_cancelled_document_is_on_the_list(self, people, trading, user):
        """It is a listing, and the cancelled one is what somebody is looking for."""
        sales.cancel_sales_invoice(trading.theirs, user=user, reason="Wrong shop entirely.")

        documents = board.build(people["Accountant"], day=DAY).documents
        cancelled = next(row for row in documents if row.code == trading.theirs.code)
        assert cancelled.status == "CANCELLED"

    def test_it_carries_no_amount(self, people, trading):
        """Deliberately. See the docstring on RecentDocument."""
        row = board.build(people["Accountant"], day=DAY).documents[0]
        assert not hasattr(row, "paisa")

    def test_receipts_and_bills_are_both_on_it(self, people, trading):
        kinds = {row.kind for row in board.build(people["Accountant"], day=DAY).documents}
        assert "Sales invoice" in kinds
        assert "Receipt" in kinds


# ===========================================================================
# The cache
# ===========================================================================
class TestTheCache:
    def test_a_second_read_comes_back_from_the_cache(self, people, trading):
        """Equal, not identical: the local-memory backend pickles what it holds."""
        first = board.dashboard_for(people["Accountant"], day=DAY)
        second = board.dashboard_for(people["Accountant"], day=DAY)
        assert first == second
        assert cache.get(board.cache_key(people["Accountant"], DAY)) is not None

    def test_a_change_is_not_seen_until_the_entry_is_dropped(
        self, people, trading, shops, warehouses, oil, user
    ):
        """Sixty seconds of staleness is the trade this page makes on purpose."""
        before = card(board.dashboard_for(people["Accountant"], day=DAY), "sales").paisa
        bill(shops.ours, warehouses.main, oil, user, rupees="1234", cartons=1)

        assert card(board.dashboard_for(people["Accountant"], day=DAY), "sales").paisa == before

        board.invalidate(people["Accountant"], day=DAY)
        after = card(board.dashboard_for(people["Accountant"], day=DAY), "sales").paisa
        assert after == before + to_paisa("1234")

    def test_two_logins_never_share_an_entry(self, people, trading):
        """The whole reason the key carries the user: it is role-aware."""
        accountant = board.dashboard_for(people["Accountant"], day=DAY)
        operator = board.dashboard_for(people["Operator"], day=DAY)

        assert accountant.cards != ()
        assert operator.cards == ()

    def test_the_key_rolls_over_at_midnight(self, people):
        today = board.cache_key(people["Accountant"], DAY)
        tomorrow = board.cache_key(people["Accountant"], DAY + dt.timedelta(days=1))
        assert today != tomorrow

    def test_nothing_is_cached_across_a_shape_change(self, people):
        """The version in the key is what a changed dataclass must miss on."""
        assert f":{board.CACHE_VERSION}:" in board.cache_key(people["Accountant"], DAY)


# ===========================================================================
# The screen
# ===========================================================================
class TestTheScreen:
    def test_the_site_root_is_the_dashboard(self, client, people, trading):
        client.force_login(people["Accountant"])
        response = client.get("/")

        assert response.status_code == 200
        assert response.templates[0].name == "reports/dashboard.html"

    def test_an_anonymous_visitor_is_sent_to_the_login(self, client):
        response = client.get("/")
        assert response.status_code == 302
        assert "login" in response["Location"]

    def test_every_card_links_to_a_report_that_resolves(self, client, people, trading):
        """A number nobody can drill into is a number nobody trusts."""
        client.force_login(people["Accountant"])
        body = client.get("/").content.decode()

        built = board.build(people["Accountant"], day=DAY)
        for entry in built.cards:
            assert entry.url, f"{entry.key} has no link"
            assert entry.url.split("?")[0] in body or entry.url in body

    def test_an_operators_page_carries_no_rupee_figure(self, client, people, trading):
        """Asserted against the response, not against the context.

        A figure hidden by a template is a figure still in the HTML, and the
        brief is that an Operator does not have one.
        """
        client.force_login(people["Operator"])
        body = client.get("/").content.decode()

        assert "Outstanding receivable" not in body
        assert "Cash and bank" not in body
        assert "Sales today" not in body
        # The operational half is still there.
        assert "Routes running today" in body
        assert "Recent documents" in body

    def test_the_chart_is_inline_svg_with_no_script_and_no_external_host(
        self, client, people, stocked, shops, warehouses, oil, user
    ):
        """CLAUDE.md §7: the production PC has no internet.

        Billed on the real ``localdate`` rather than on ``DAY``, because the
        screen is always today and a trend with nothing in its window draws the
        empty state rather than a line.
        """
        bill(shops.ours, warehouses.main, oil, user, on=timezone.localdate(), rupees="1000")
        client.force_login(people["Accountant"])

        body = client.get("/").content.decode()

        assert "<polyline" in body
        assert "<svg" in body
        assert "http://" not in body
        assert "https://" not in body

    def test_the_chase_list_offers_a_click_to_call(
        self, client, people, stocked, shops, warehouses, oil, user
    ):
        bill(
            shops.ours,
            warehouses.main,
            oil,
            user,
            rupees="800",
            on=DAY - dt.timedelta(days=80),
            due_date=DAY - dt.timedelta(days=80),
        )
        client.force_login(people["Accountant"])
        cache.clear()

        body = client.get("/").content.decode()
        assert 'href="tel:03002214477"' in body

    def test_the_sidebar_offers_it_to_everybody_who_may_open_it(self, client, people):
        """A menu never offers what a click would refuse, nor hides what it would allow.

        Every seeded group holds ``reports.view_reports``, which is the one
        permission both this link and the view behind it read.
        """
        for name in ("Accountant", "Operator", "Booker", "Viewer", "Admin"):
            client.force_login(people[name])
            body = client.get(reverse("reports:index")).content.decode()
            assert "Dashboard" in body, f"{name} is not offered the dashboard"
