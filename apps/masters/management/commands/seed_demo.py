"""Build a realistic set of masters to develop against.

Six routes across the week, eight sellers spread over them, sixty shops, ten
suppliers and forty items with the packing sizes a real distributor carries —
cartons of 12, 24 and 48, and the loose ``carton_size = 1`` goods that are the
easiest thing in the system to get wrong.

Three properties, all deliberate:

* **Idempotent.** Every row goes through ``get_or_create`` keyed on its code, so
  running this twice creates nothing the second time and never overwrites an
  edit someone made in the admin.
* **Deterministic.** Nothing is random. Every varied value is picked by index
  from a fixed list, so the dataset is identical on every machine and a bug
  found against client C-0042 is still there tomorrow.
* **Additive only.** Nothing is deleted. Masters acquire ledger history the
  moment anything is posted against them, and a seeder that cleared the table
  first would be a seeder that could destroy a day's work.

It writes no ledger and no stock rows — those come from posting documents, which
is what the transaction apps are for.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.core.money import to_paisa
from apps.masters.enums import DayOfWeek, Unit
from apps.masters.models import (
    Client,
    Item,
    ItemCategory,
    Route,
    RouteSeller,
    Seller,
    Vendor,
)

# ---------------------------------------------------------------------------
# Categories: (name, parent name or None)
# ---------------------------------------------------------------------------
CATEGORIES = [
    ("Food", None),
    ("Non-Food", None),
    ("Beverages", "Food"),
    ("Dairy", "Food"),
    ("Snacks", "Food"),
    ("Staples", "Food"),
    ("Personal Care", "Non-Food"),
    ("Home Care", "Non-Food"),
]

# ---------------------------------------------------------------------------
# Items: (code, name, category, carton_size, purchase rupees, sale rupees, tax bp)
#
# Rates are per BASE UNIT — one piece, not one carton. Rupees are strings
# because to_paisa refuses a float (CLAUDE.md §1).
#
# Carton sizes are 12, 24, 48 and 1. The 1s are the interesting ones: a 25kg
# flour bag has no carton, and anything that formats quantities by dividing
# without checking allows_carton will print "17 ctn" for seventeen bags.
#
# Unprocessed staples are zero-rated; everything else carries 17.5% (1750 bp).
# ---------------------------------------------------------------------------

#: How much stock the demo says is "getting low", as a number of cartons. Only
#: so the dashboard's low-stock panel has something to show on a seeded
#: database; a real installation sets a level per item, which is the whole
#: reason the field is per item.
DEMO_REORDER_CARTONS = 2

ITEMS = [
    # Beverages
    ("TEA-190", "Tapal Danedar Tea 190g", "Beverages", 24, "382.00", "425.00", 1750),
    ("TEA-450", "Tapal Danedar Tea 450g", "Beverages", 12, "895.00", "985.00", 1750),
    ("TEABAG-100", "Lipton Yellow Label 100 Tea Bags", "Beverages", 12, "610.00", "680.00", 1750),
    ("JUICE-200", "Fruita Vitals Red Grape 200ml", "Beverages", 24, "52.00", "60.00", 1750),
    ("JUICE-1000", "Fruita Vitals Chaunsa 1L", "Beverages", 12, "268.00", "300.00", 1750),
    ("COLA-345", "Coca-Cola Can 345ml", "Beverages", 24, "68.00", "80.00", 1750),
    ("COLA-1500", "Coca-Cola 1.5L PET", "Beverages", 12, "148.00", "170.00", 1750),
    ("WATER-1500", "Nestle Pure Life 1.5L", "Beverages", 12, "62.00", "75.00", 1750),
    # Dairy
    ("MILK-1000", "Olper's Milk 1L", "Dairy", 12, "265.00", "290.00", 1750),
    ("MILK-250", "Olper's Milk 250ml", "Dairy", 24, "72.00", "82.00", 1750),
    ("CREAM-200", "Olper's Cream 200ml", "Dairy", 24, "128.00", "145.00", 1750),
    ("BUTTER-227", "Blue Band Margarine 227g", "Dairy", 24, "245.00", "275.00", 1750),
    ("CHEESE-200", "Adam's Cheddar Cheese 200g", "Dairy", 12, "395.00", "440.00", 1750),
    ("PWDR-390", "Nido Fortified Milk Powder 390g", "Dairy", 12, "735.00", "810.00", 1750),
    # Snacks
    ("BISC-SOO", "Sooper Biscuit Family Pack", "Snacks", 24, "118.00", "135.00", 1750),
    ("BISC-GALA", "Gala Biscuit Ticky Pack", "Snacks", 48, "22.00", "27.00", 1750),
    ("BISC-PRN", "Prince Biscuit Roll", "Snacks", 48, "38.00", "45.00", 1750),
    ("BISC-CAND", "Candi Biscuit Half Roll", "Snacks", 48, "34.00", "40.00", 1750),
    ("CHIPS-LAY", "Lay's Masala 40g", "Snacks", 48, "42.00", "50.00", 1750),
    ("CHIPS-KUR", "Kurkure Chutney Chaska 30g", "Snacks", 48, "26.00", "30.00", 1750),
    ("CHOC-DM", "Dairy Milk 38g", "Snacks", 48, "128.00", "150.00", 1750),
    ("CANDY-CM", "Chilli Milli Candy Jar", "Snacks", 12, "410.00", "470.00", 1750),
    ("NIMKO-200", "Mixed Nimko 200g", "Snacks", 24, "165.00", "190.00", 1750),
    # Staples — the loose ones, sold by the bag
    ("RICE-5", "Basmati Rice 5kg Bag", "Staples", 1, "1620.00", "1780.00", 0),
    ("RICE-25", "Basmati Rice 25kg Bag", "Staples", 1, "7850.00", "8400.00", 0),
    ("ATTA-10", "Chakki Atta 10kg Bag", "Staples", 1, "1150.00", "1260.00", 0),
    ("SUGAR-50", "Refined Sugar 50kg Bag", "Staples", 1, "7100.00", "7500.00", 0),
    ("DAL-CHN", "Chana Daal 1kg", "Staples", 24, "285.00", "320.00", 0),
    ("DAL-MSR", "Masoor Daal 1kg", "Staples", 24, "310.00", "345.00", 0),
    ("SALT-800", "Iodised Salt 800g", "Staples", 24, "42.00", "50.00", 0),
    ("OIL-1000", "Dalda Cooking Oil 1L", "Staples", 12, "545.00", "595.00", 1750),
    ("GHEE-1000", "Dalda Banaspati 1kg", "Staples", 12, "580.00", "635.00", 1750),
    ("GHEE-5000", "Dalda Banaspati 5kg Tin", "Staples", 1, "2790.00", "3020.00", 1750),
    # Personal care
    ("SOAP-LUX", "Lux Soap 100g", "Personal Care", 48, "128.00", "148.00", 1750),
    ("SOAP-SFG", "Safeguard Soap 130g", "Personal Care", 48, "158.00", "180.00", 1750),
    ("SHMP-HS", "Head & Shoulders 185ml", "Personal Care", 24, "545.00", "610.00", 1750),
    ("TP-CG", "Colgate Toothpaste 100g", "Personal Care", 48, "215.00", "245.00", 1750),
    ("DIAP-M", "Pampers Medium Mega Pack", "Personal Care", 1, "1980.00", "2200.00", 1750),
    # Home care
    ("DET-SRF", "Surf Excel 1kg", "Home Care", 12, "610.00", "680.00", 1750),
    ("DISH-VIM", "Vim Dishwash Bar 200g", "Home Care", 48, "58.00", "68.00", 1750),
]

# ---------------------------------------------------------------------------
# Routes: (code, name, day). Six beats, Monday to Saturday.
# ---------------------------------------------------------------------------
ROUTES = [
    ("R-01", "Saddar & City", DayOfWeek.MON),
    ("R-02", "Gulshan-e-Iqbal", DayOfWeek.TUE),
    ("R-03", "North Nazimabad", DayOfWeek.WED),
    ("R-04", "Korangi & Landhi", DayOfWeek.THU),
    ("R-05", "Clifton & DHA", DayOfWeek.FRI),
    ("R-06", "Malir & Shah Faisal", DayOfWeek.SAT),
]

# ---------------------------------------------------------------------------
# Sellers: (code, name, phone). Eight of them for six routes.
# ---------------------------------------------------------------------------
SELLERS = [
    ("S-01", "Imran Qureshi", "0300-2214477"),
    ("S-02", "Bilal Ahmed", "0301-3345566"),
    ("S-03", "Kashif Raza", "0302-8876543"),
    ("S-04", "Adnan Siddiqui", "0321-4433221"),
    ("S-05", "Naveed Alam", "0333-7788990"),
    ("S-06", "Zeeshan Malik", "0345-1122334"),
    ("S-07", "Faisal Iqbal", "0311-6655443"),
    ("S-08", "Usman Ghani", "0346-9988776"),
]

#: (route index, seller index, is_primary). Every route has exactly one primary.
#: Two busy beats carry a second booker, and S-01 works a second route on
#: Saturday — the many-to-many is real in both directions, and the demo data
#: should exercise it rather than describe it.
ROUTE_SELLERS = [
    (0, 0, True),
    (1, 1, True),
    (2, 2, True),
    (3, 3, True),
    (4, 4, True),
    (5, 5, True),
    (0, 6, False),
    (1, 7, False),
    (5, 0, False),
]

# ---------------------------------------------------------------------------
# Clients: 15 shop names x 4 shop types = 60 distinct shops, dealt round-robin
# onto the six routes so each beat carries exactly ten.
# ---------------------------------------------------------------------------
SHOP_NAMES = [
    "Al-Madina",
    "New Sabir",
    "Bismillah",
    "Rehmat",
    "Chaudhry",
    "Khan Brothers",
    "Ideal",
    "Naya Daur",
    "Al-Habib",
    "Shaheen",
    "Faisal",
    "Mehran",
    "Noor",
    "Sunny",
    "Data",
]
SHOP_TYPES = ["Kiryana Store", "General Store", "Super Mart", "Cash & Carry"]

#: Area per route, so a shop's city column matches the beat it is on.
ROUTE_AREAS = [
    "Saddar",
    "Gulshan-e-Iqbal",
    "North Nazimabad",
    "Korangi",
    "Clifton",
    "Malir",
]

#: Cycled by index. A COD shop has no limit; the rest run on a week to a month.
CREDIT_TERMS = [
    (0, "0"),  # cash on delivery
    (7, "50000"),
    (15, "120000"),
    (30, "250000"),
    (7, "75000"),
]

#: Cycled by index. Most shops opened square; a few carried a balance in.
OPENING_BALANCES = ["0", "0", "0", "12500.50", "0", "48200", "0", "3750.25"]

# ---------------------------------------------------------------------------
# Vendors: (code, name, city, credit days, credit limit rupees)
# ---------------------------------------------------------------------------
VENDORS = [
    ("V-01", "Unilever Pakistan Ltd", "Karachi", 30, "5000000"),
    ("V-02", "Nestle Pakistan Ltd", "Lahore", 30, "4000000"),
    ("V-03", "National Foods Ltd", "Karachi", 21, "2500000"),
    ("V-04", "Tapal Tea (Pvt) Ltd", "Karachi", 15, "3000000"),
    ("V-05", "English Biscuit Manufacturers", "Karachi", 21, "2000000"),
    ("V-06", "Colgate-Palmolive Pakistan", "Karachi", 30, "1800000"),
    ("V-07", "Procter & Gamble Pakistan", "Karachi", 30, "3500000"),
    ("V-08", "Dalda Foods (Pvt) Ltd", "Karachi", 15, "2200000"),
    ("V-09", "Coca-Cola Beverages Pakistan", "Karachi", 7, "1500000"),
    ("V-10", "Continental Biscuits Ltd", "Sukkur", 21, "1200000"),
]


class Command(BaseCommand):
    help = "Create a realistic demo dataset of items, parties, routes and sellers."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be created and roll back.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        created: dict[str, int] = {}

        # One transaction for the lot: a half-seeded database — routes with no
        # sellers, clients pointing at routes that were not written — is worse
        # to debug than an empty one.
        with transaction.atomic():
            categories = self._seed_categories(created)
            self._seed_items(created, categories)
            routes = self._seed_routes(created)
            sellers = self._seed_sellers(created)
            self._seed_route_sellers(created, routes, sellers)
            self._seed_clients(created, routes, sellers)
            self._seed_vendors(created)
            self._seed_default_warehouse(created)
            if dry_run:
                transaction.set_rollback(True)

        verb = "Would create" if dry_run else "Created"
        for label, count in created.items():
            self.stdout.write(f"  {count:>3} {label}")
        self.stdout.write(
            self.style.SUCCESS(
                f"{verb} {sum(created.values())} record(s). "
                f"Anything already present was left exactly as it was."
            )
        )

    # ------------------------------------------------------------------
    # Steps
    # ------------------------------------------------------------------
    def _seed_categories(self, created) -> dict[str, ItemCategory]:
        by_name: dict[str, ItemCategory] = {}
        count = 0
        # CATEGORIES lists parents before children, so a parent is always in
        # by_name by the time a child asks for it.
        for name, parent_name in CATEGORIES:
            category, was_created = ItemCategory.objects.get_or_create(
                name=name,
                parent=by_name[parent_name] if parent_name else None,
            )
            by_name[name] = category
            if was_created:
                count += 1
        created["categories"] = count
        return by_name

    def _seed_items(self, created, categories) -> None:
        count = 0
        for code, name, category_name, carton_size, purchase, sale, tax_bp in ITEMS:
            _, was_created = Item.objects.get_or_create(
                code=code,
                defaults={
                    "name": name,
                    "category": categories[category_name],
                    "base_unit": Unit.PIECE,
                    "carton_size": carton_size,
                    "purchase_rate_paisa": to_paisa(purchase),
                    "sale_rate_paisa": to_paisa(sale),
                    "tax_rate_bp": tax_bp,
                    # Derived rather than a column in ITEMS, because a demo
                    # reorder level is not a fact about the goods — it is "two
                    # cartons left" expressed in the base units the field
                    # stores. A real installation types its own per item.
                    "reorder_level_pieces": carton_size * DEMO_REORDER_CARTONS,
                },
            )
            if was_created:
                count += 1
        created["items"] = count

    def _seed_routes(self, created) -> list[Route]:
        routes = []
        count = 0
        for code, name, day in ROUTES:
            route, was_created = Route.objects.get_or_create(
                code=code,
                defaults={"name": name, "day_of_week": day},
            )
            routes.append(route)
            if was_created:
                count += 1
        created["routes"] = count
        return routes

    def _seed_sellers(self, created) -> list[Seller]:
        sellers = []
        count = 0
        for code, name, phone in SELLERS:
            seller, was_created = Seller.objects.get_or_create(
                code=code,
                defaults={"name": name, "phone": phone},
            )
            sellers.append(seller)
            if was_created:
                count += 1
        created["sellers"] = count
        return sellers

    def _seed_route_sellers(self, created, routes, sellers) -> None:
        count = 0
        for route_index, seller_index, is_primary in ROUTE_SELLERS:
            route = routes[route_index]
            # Only claim primary if the route has not already been given one by
            # hand: get_or_create would otherwise trip the partial unique index
            # on a database someone has already edited.
            wanted_primary = (
                is_primary and not RouteSeller.objects.filter(route=route, is_primary=True).exists()
            )
            _, was_created = RouteSeller.objects.get_or_create(
                route=route,
                seller=sellers[seller_index],
                defaults={"is_primary": wanted_primary},
            )
            if was_created:
                count += 1
        created["route sellers"] = count

    def _seed_clients(self, created, routes, sellers) -> None:
        #: route -> its sellers, primary first. Every fifth shop on a beat is
        #: booked by the second seller where there is one.
        sellers_by_route: dict[int, list[Seller]] = {}
        for route in routes:
            links = sorted(
                RouteSeller.objects.filter(route=route).select_related("seller"),
                key=lambda link: (not link.is_primary, link.seller.code),
            )
            sellers_by_route[route.pk] = [link.seller for link in links]

        count = 0
        for index in range(len(SHOP_NAMES) * len(SHOP_TYPES)):
            route = routes[index % len(routes)]
            route_sellers = sellers_by_route[route.pk] or sellers

            # Every fifth shop on a beat is booked by the route's second seller
            # where there is one — a route whose whole client list points at one
            # person would never catch a report that ignores the field.
            position_on_route = index // len(routes)
            use_second = len(route_sellers) > 1 and position_on_route % 5 == 4
            seller = route_sellers[1] if use_second else route_sellers[0]

            credit_days, credit_limit = CREDIT_TERMS[index % len(CREDIT_TERMS)]
            _, was_created = Client.objects.get_or_create(
                code=f"C-{index + 1:04d}",
                defaults={
                    "name": f"{SHOP_NAMES[index % len(SHOP_NAMES)]} "
                    f"{SHOP_TYPES[index // len(SHOP_NAMES)]}",
                    "phone": f"03{index % 10}{index % 7}-{1000000 + index * 8461:07d}",
                    "address": f"Shop {index % 40 + 1}, Block {chr(65 + index % 6)}",
                    "city": ROUTE_AREAS[index % len(ROUTE_AREAS)],
                    "route": route,
                    "seller": seller,
                    "opening_balance_paisa": to_paisa(
                        OPENING_BALANCES[index % len(OPENING_BALANCES)]
                    ),
                    "credit_limit_paisa": to_paisa(credit_limit),
                    "credit_days": credit_days,
                },
            )
            if was_created:
                count += 1
        created["clients"] = count

    def _seed_vendors(self, created) -> None:
        count = 0
        for code, name, city, credit_days, credit_limit in VENDORS:
            _, was_created = Vendor.objects.get_or_create(
                code=code,
                defaults={
                    "name": name,
                    "city": city,
                    "phone": f"021-3{code[-2:]}{code[-2:]}000",
                    "address": f"{name}, {city}",
                    "credit_days": credit_days,
                    "credit_limit_paisa": to_paisa(credit_limit),
                },
            )
            if was_created:
                count += 1
        created["vendors"] = count

    def _seed_default_warehouse(self, created) -> None:
        """Somewhere for the demo stock to live.

        The one thing here that is not a master of this app, and it earns its
        place: ``Warehouse.get_default()`` raises when no warehouse is flagged,
        so a dataset without one cannot receive a single stock row. Guarded so
        it never fights a default someone has already chosen — the partial
        unique index on ``is_default`` would reject a second one.
        """
        from apps.accounting.models import Warehouse

        _, was_created = Warehouse.objects.get_or_create(
            code="MAIN",
            defaults={
                "name": "Main Godown",
                "is_default": not Warehouse.objects.filter(is_default=True).exists(),
            },
        )
        created["warehouses"] = 1 if was_created else 0
