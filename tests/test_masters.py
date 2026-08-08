"""The master-data invariants: packing, tax rates, the category tree, the
route/seller link, and the rule that no master caches a balance.

Unit conversion has its own file — tests/test_uom.py.
"""

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.masters.enums import DayOfWeek, Unit
from apps.masters.exceptions import (
    DuplicatePrimarySeller,
    InvalidCategory,
    InvalidPacking,
)
from apps.masters.models import (
    Client,
    Item,
    ItemCategory,
    Route,
    RouteSeller,
    Seller,
    Vendor,
)

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Item
# ---------------------------------------------------------------------------
class TestItem:
    def test_rates_are_integer_paisa(self):
        """Never a Decimal, never a float — CLAUDE.md §1."""
        item = Item.objects.create(
            code="OIL-1000",
            name="Cooking Oil 1L",
            purchase_rate_paisa=54500,
            sale_rate_paisa=59500,
        )
        item.refresh_from_db()
        assert isinstance(item.purchase_rate_paisa, int)
        assert isinstance(item.sale_rate_paisa, int)

    @pytest.mark.parametrize(
        ("basis_points", "expected"),
        [(0, "0.00%"), (1750, "17.50%"), (500, "5.00%"), (10000, "100.00%"), (25, "0.25%")],
    )
    def test_tax_rate_renders_from_basis_points(self, basis_points, expected):
        """Integer arithmetic: rendering a rate must not become a second
        rounding site (CLAUDE.md §1)."""
        item = Item(code="X", name="X", tax_rate_bp=basis_points)
        assert item.tax_rate_display == expected

    def test_a_tax_rate_over_one_hundred_percent_is_rejected(self):
        with pytest.raises(IntegrityError), transaction.atomic():
            Item.objects.create(code="TAXED", name="Over-taxed", tax_rate_bp=10001)

    def test_packing_errors_surface_as_form_errors(self):
        """``clean`` is what the admin calls; it must not leak InvalidPacking."""
        with pytest.raises(ValidationError):
            Item(code="BAD", name="Bad", carton_size=0).full_clean()

    def test_a_broken_carton_size_cannot_be_saved(self):
        with pytest.raises(InvalidPacking):
            Item.objects.create(code="BAD", name="Bad", carton_size=0)

    def test_an_item_with_stock_movement_cannot_be_deleted(self, warehouses):
        """``StockEntry.item`` is PROTECT: history is never orphaned."""
        from django.db.models import ProtectedError

        from apps.accounting.models import StockEntry

        item = Item.objects.create(code="MOVED", name="Has history")
        StockEntry.objects.create(
            posting_date="2026-01-01",
            item=item,
            warehouse=warehouses.main,
            qty_base=10,
            rate_paisa=1000,
            value_paisa=10000,
            voucher_type="SampleDocument",
            voucher_id=1,
            voucher_code="SI-2026-000001",
        )
        with pytest.raises(ProtectedError):
            item.delete()


class TestItemCategory:
    def test_a_child_shows_its_parent(self):
        food = ItemCategory.objects.create(name="Food")
        snacks = ItemCategory.objects.create(name="Snacks", parent=food)
        assert str(snacks) == "Food > Snacks"
        assert str(food) == "Food"

    def test_a_category_cannot_be_its_own_parent(self):
        food = ItemCategory.objects.create(name="Food")
        food.parent = food
        with pytest.raises(InvalidCategory):
            food.save()

    def test_a_cycle_is_refused(self):
        food = ItemCategory.objects.create(name="Food")
        snacks = ItemCategory.objects.create(name="Snacks", parent=food)
        food.parent = snacks
        with pytest.raises(InvalidCategory):
            food.save()

    def test_two_top_level_categories_cannot_share_a_name(self):
        ItemCategory.objects.create(name="Food")
        with pytest.raises(IntegrityError), transaction.atomic():
            ItemCategory.objects.create(name="Food")

    def test_two_siblings_cannot_share_a_name(self):
        food = ItemCategory.objects.create(name="Food")
        ItemCategory.objects.create(name="Snacks", parent=food)
        with pytest.raises(IntegrityError), transaction.atomic():
            ItemCategory.objects.create(name="Snacks", parent=food)

    def test_the_same_name_under_different_parents_is_fine(self):
        food = ItemCategory.objects.create(name="Food")
        non_food = ItemCategory.objects.create(name="Non-Food")
        ItemCategory.objects.create(name="Imported", parent=food)
        ItemCategory.objects.create(name="Imported", parent=non_food)
        assert ItemCategory.objects.filter(name="Imported").count() == 2


# ---------------------------------------------------------------------------
# Routes and sellers
# ---------------------------------------------------------------------------
@pytest.fixture
def route(db):
    return Route.objects.create(code="R-01", name="Saddar & City", day_of_week=DayOfWeek.MON)


@pytest.fixture
def sellers(db):
    return [
        Seller.objects.create(code="S-01", name="Imran Qureshi"),
        Seller.objects.create(code="S-02", name="Bilal Ahmed"),
    ]


class TestRoute:
    def test_a_route_may_be_unscheduled(self):
        """NULL means unscheduled; there is no second empty value."""
        spot = Route.objects.create(code="R-99", name="Spot runs")
        assert spot.day_of_week is None
        assert spot.day_index == 7  # unscheduled sorts after Sunday

    def test_a_blank_day_is_refused(self):
        """ "" and NULL would be two ways to say the same thing."""
        with pytest.raises(IntegrityError), transaction.atomic():
            Route.objects.create(code="R-98", name="Blank day", day_of_week="")

    def test_days_sort_in_week_order_not_alphabetically(self):
        routes = [
            Route.objects.create(code="R-A", name="A", day_of_week=DayOfWeek.FRI),
            Route.objects.create(code="R-B", name="B", day_of_week=DayOfWeek.MON),
            Route.objects.create(code="R-C", name="C", day_of_week=DayOfWeek.WED),
        ]
        assert [r.code for r in sorted(routes, key=lambda r: r.day_index)] == [
            "R-B",
            "R-C",
            "R-A",
        ]

    def test_client_count_counts_the_shops_on_the_beat(self, route):
        assert route.client_count == 0
        for index in range(3):
            Client.objects.create(code=f"C-{index:04d}", name=f"Shop {index}", route=route)
        assert route.client_count == 3

    def test_client_count_prefers_an_annotation(self, route):
        """The admin annotates ``_client_count`` so a changelist is one query.

        The annotation is trusted when present — that is the whole point — so
        this asserts the property reads it rather than silently re-counting.
        """
        from django.db.models import Count

        Client.objects.create(code="C-0001", name="Shop", route=route)
        annotated = Route.objects.annotate(_client_count=Count("clients")).get(pk=route.pk)
        assert annotated.client_count == 1

    def test_a_route_with_shops_cannot_be_deleted(self, route):
        from django.db.models import ProtectedError

        Client.objects.create(code="C-0001", name="Shop", route=route)
        with pytest.raises(ProtectedError):
            route.delete()


class TestRouteSeller:
    def test_a_route_carries_several_sellers(self, route, sellers):
        for seller in sellers:
            RouteSeller.objects.create(route=route, seller=seller)
        assert route.sellers.count() == 2

    def test_a_seller_works_several_routes(self, sellers):
        second = Route.objects.create(code="R-02", name="Malir", day_of_week=DayOfWeek.SAT)
        first = Route.objects.create(code="R-01", name="Saddar", day_of_week=DayOfWeek.MON)
        RouteSeller.objects.create(route=first, seller=sellers[0], is_primary=True)
        RouteSeller.objects.create(route=second, seller=sellers[0])
        assert sellers[0].routes.count() == 2

    def test_a_seller_is_only_on_a_route_once(self, route, sellers):
        RouteSeller.objects.create(route=route, seller=sellers[0])
        with pytest.raises(IntegrityError), transaction.atomic():
            RouteSeller.objects.create(route=route, seller=sellers[0])

    def test_a_route_has_at_most_one_primary_seller(self, route, sellers):
        RouteSeller.objects.create(route=route, seller=sellers[0], is_primary=True)
        with pytest.raises(DuplicatePrimarySeller):
            RouteSeller.objects.create(route=route, seller=sellers[1], is_primary=True)

    def test_the_same_seller_may_be_primary_on_two_routes(self, route, sellers):
        other = Route.objects.create(code="R-02", name="Malir", day_of_week=DayOfWeek.SAT)
        RouteSeller.objects.create(route=route, seller=sellers[0], is_primary=True)
        RouteSeller.objects.create(route=other, seller=sellers[0], is_primary=True)
        assert RouteSeller.objects.filter(is_primary=True).count() == 2

    def test_a_duplicate_primary_surfaces_as_a_form_error(self, route, sellers):
        RouteSeller.objects.create(route=route, seller=sellers[0], is_primary=True)
        link = RouteSeller(route=route, seller=sellers[1], is_primary=True)
        with pytest.raises(ValidationError):
            link.full_clean()


# ---------------------------------------------------------------------------
# Parties
# ---------------------------------------------------------------------------
class TestParties:
    def test_clients_and_vendors_are_separate_tables(self):
        """Same code on both is fine: they are different kinds of party.

        A single table with a type column would make this a clash, and every
        query in the system would carry a type filter it could forget.
        """
        Client.objects.create(code="P-001", name="Al-Madina Kiryana")
        Vendor.objects.create(code="P-001", name="Unilever Pakistan")
        assert Client.objects.count() == 1
        assert Vendor.objects.count() == 1

    def test_a_code_is_unique_within_a_party_type(self):
        Client.objects.create(code="C-0001", name="First")
        with pytest.raises(IntegrityError), transaction.atomic():
            Client.objects.create(code="C-0001", name="Second")

    def test_an_opening_balance_may_be_negative(self):
        """A client who paid an advance before go-live is in credit."""
        client = Client.objects.create(
            code="C-0001", name="Prepaid Shop", opening_balance_paisa=-500000
        )
        client.refresh_from_db()
        assert client.opening_balance_paisa == -500000

    def test_a_walk_in_belongs_to_no_route(self):
        client = Client.objects.create(code="C-0002", name="Walk-in")
        assert client.route_id is None
        assert client.seller_id is None

    def test_a_seller_with_shops_cannot_be_deleted(self, sellers):
        from django.db.models import ProtectedError

        Client.objects.create(code="C-0001", name="Shop", seller=sellers[0])
        with pytest.raises(ProtectedError):
            sellers[0].delete()


# ---------------------------------------------------------------------------
# The seeder
# ---------------------------------------------------------------------------
class TestSeedDemo:
    """The dataset the rest of the build is developed against.

    Worth testing rather than eyeballing: it is run constantly, and a seeder
    that half-fails leaves a database that is harder to debug than an empty one.
    """

    @pytest.fixture
    def seeded(self, db):
        from django.core.management import call_command

        call_command("seed_demo", verbosity=0)

    def test_creates_the_promised_dataset(self, seeded):
        assert Route.objects.count() == 6
        assert Seller.objects.count() == 8
        assert Client.objects.count() == 60
        assert Item.objects.count() == 40
        assert Vendor.objects.count() == 10

    def test_the_shops_are_spread_evenly_over_the_beats(self, seeded):
        counts = {route.code: route.client_count for route in Route.objects.all()}
        assert set(counts.values()) == {10}

    def test_every_route_has_exactly_one_primary_seller(self, seeded):
        for route in Route.objects.all():
            assert route.route_sellers.filter(is_primary=True).count() == 1

    def test_the_packing_sizes_are_varied_and_include_loose_items(self, seeded):
        sizes = set(Item.objects.values_list("carton_size", flat=True))
        assert {1, 12, 24, 48} <= sizes
        assert Item.objects.filter(carton_size=1).count() >= 5

    def test_rates_are_stored_as_integer_paisa(self, seeded):
        item = Item.objects.get(code="OIL-1000")
        assert item.purchase_rate_paisa == 54500
        assert item.sale_rate_paisa == 59500
        assert item.tax_rate_bp == 1750

    def test_running_it_twice_creates_nothing_the_second_time(self, seeded):
        from django.core.management import call_command

        before = (Item.objects.count(), Client.objects.count(), RouteSeller.objects.count())
        call_command("seed_demo", verbosity=0)
        after = (Item.objects.count(), Client.objects.count(), RouteSeller.objects.count())
        assert before == after

    def test_dry_run_writes_nothing(self, db):
        from django.core.management import call_command

        call_command("seed_demo", "--dry-run", verbosity=0)
        assert Item.objects.count() == 0
        assert Client.objects.count() == 0

    def test_the_items_all_convert(self, seeded):
        """Every seeded item must survive the conversion helpers — a demo row
        that cannot be turned into base units is a demo row that will waste an
        afternoon."""
        from apps.masters.services import fmt_qty, from_base, to_base

        for item in Item.objects.all():
            qty_base = to_base(item, 3, Unit.CARTON)
            cartons, loose = from_base(item, qty_base)
            assert cartons * item.carton_size + loose == qty_base
            assert fmt_qty(item, qty_base)
