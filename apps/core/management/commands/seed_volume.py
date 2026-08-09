"""``python manage.py seed_volume --invoices 50000``

Fill a **scratch** database with a realistic year of trading, so the reports can
be profiled against something the size of a real installation rather than
against the dozen rows a test fixture makes.

This is a development tool. It refuses to run against the production settings,
because a command whose entire purpose is to write fifty thousand fake invoices
should not be one typo away from doing it to somebody's books.

**It bypasses the posting services on purpose**, and that is the one thing to
understand before trusting a number that comes out of it. Posting 50,000
invoices properly means 50,000 transactions, each taking SQLite's write lock —
which measures the *posting* path, not the reporting path this exists to
profile, and takes about an hour. So the rows are written with ``bulk_create``
in balanced pairs.

What that costs: the data is shaped like real data and balances like real data,
but it did not go through the guards. Never point this at a database anybody
cares about, and never quote a *posting* benchmark from it.
"""

from __future__ import annotations

import datetime as dt
import random
import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.accounting.chart import seed_chart_of_accounts
from apps.accounting.enums import PartyType
from apps.accounting.models import Account, LedgerEntry, StockEntry, Warehouse
from apps.core.enums import DocumentStatus
from apps.masters.models import Client, Item, Route, Seller, Vendor
from apps.sales.models import SalesInvoice

BATCH = 2_000


class Command(BaseCommand):
    help = "Fill a scratch database with a realistic year of trading, for profiling."

    def add_arguments(self, parser):
        parser.add_argument("--invoices", type=int, default=50_000)
        parser.add_argument("--clients", type=int, default=600)
        parser.add_argument("--items", type=int, default=400)
        parser.add_argument("--seed", type=int, default=20260809, help="For repeatable runs.")
        parser.add_argument(
            "--settled-fraction",
            type=float,
            default=0.85,
            help=(
                "How much of the year has been collected. 0.85 is what a "
                "distribution business looks like; 0 is the pathological case "
                "where nothing has ever been paid, which is worth measuring "
                "once and is not what anybody's screen does."
            ),
        )

    def handle(self, *args, **options):
        # Allow-list, not a deny-list. "refuse if it looks like production" is
        # one renamed settings module away from writing 200,000 fake ledger rows
        # into somebody's books, and those rows are append-only (CLAUDE.md §3) —
        # there is no delete to undo them with.
        #
        # This file is also removed from the Windows release by `make
        # build-release`, so on the office PC the command does not exist at all.
        # This is the second lock on the same door.
        module = settings.SETTINGS_MODULE or ""
        if not module.endswith(".profile"):
            raise CommandError(
                f"Refusing to run under {module or 'unknown settings'}.\n"
                "\n"
                "This command writes tens of thousands of fake invoices directly into "
                "the ledger, which is append-only — there is no way to delete them.\n"
                "\n"
                "It only runs against the scratch database:\n"
                "    manage.py seed_volume --settings=config.settings.profile"
            )

        rng = random.Random(options["seed"])
        started = time.monotonic()

        seed_chart_of_accounts(Account)
        accounts = {a.code: a for a in Account.objects.all()}
        warehouse = Warehouse.objects.filter(is_default=True).first() or Warehouse.objects.create(
            code="MAIN", name="Main Godown", is_default=True
        )

        routes = self._routes()
        sellers = self._sellers()
        clients = self._clients(options["clients"], routes, sellers, rng)
        items = self._items(options["items"], rng)
        self._vendor()

        self.stdout.write(f"masters: {len(clients)} clients, {len(items)} items")

        invoices = self._invoices(options["invoices"], clients, warehouse, rng)
        self.stdout.write(f"invoices: {len(invoices)}")

        entries, movements = self._ledger(invoices, items, accounts, warehouse, rng)
        self.stdout.write(f"ledger: {entries} entries, {movements} stock rows")

        settled = self._receipts(invoices, accounts, rng, options["settled_fraction"])
        self.stdout.write(f"receipts: {settled} invoices settled")

        self.stdout.write(self.style.SUCCESS(f"\nSeeded in {time.monotonic() - started:.1f}s."))

    # ------------------------------------------------------------------
    def _routes(self):
        existing = list(Route.objects.all())
        if existing:
            return existing
        return Route.objects.bulk_create(
            [Route(code=f"R-{n:02d}", name=f"Route {n}") for n in range(1, 13)]
        ) or list(Route.objects.all())

    def _sellers(self):
        existing = list(Seller.objects.all())
        if existing:
            return existing
        Seller.objects.bulk_create(
            [Seller(code=f"S-{n:02d}", name=f"Seller {n}") for n in range(1, 13)]
        )
        return list(Seller.objects.all())

    def _vendor(self):
        Vendor.objects.get_or_create(code="V-0001", defaults={"name": "Bulk Supplier"})

    def _clients(self, count, routes, sellers, rng):
        have = Client.objects.count()
        if have < count:
            Client.objects.bulk_create(
                [
                    Client(
                        code=f"C-{n:05d}",
                        name=f"Shop {n}",
                        route=rng.choice(routes),
                        seller=rng.choice(sellers),
                        credit_limit_paisa=rng.choice([200_000_00, 500_000_00, 1_000_000_00]),
                        credit_days=rng.choice([7, 15, 30]),
                    )
                    for n in range(have, count)
                ],
                batch_size=BATCH,
            )
        return list(Client.objects.values_list("pk", flat=True))

    def _items(self, count, rng):
        have = Item.objects.count()
        if have < count:
            Item.objects.bulk_create(
                [
                    Item(
                        code=f"ITM-{n:05d}",
                        name=f"Item {n}",
                        carton_size=rng.choice([1, 6, 12, 24]),
                        sale_rate_paisa=rng.randrange(5_000, 500_000, 500),
                    )
                    for n in range(have, count)
                ],
                batch_size=BATCH,
            )
        return list(Item.objects.values_list("pk", flat=True))

    def _invoices(self, count, clients, warehouse, rng):
        """Headers only, spread over a year, mostly posted with some cancelled."""
        start = dt.date(2026, 1, 1)
        have = SalesInvoice.objects.count()
        rows = []
        for n in range(have, count):
            day = start + dt.timedelta(days=rng.randrange(0, 365))
            total = rng.randrange(50_000, 5_000_000, 100)
            rows.append(
                SalesInvoice(
                    code=f"SI-2026-{n + 1:06d}",
                    status=(
                        DocumentStatus.CANCELLED if rng.random() < 0.03 else DocumentStatus.POSTED
                    ),
                    posting_date=day,
                    due_date=day + dt.timedelta(days=15),
                    client_id=rng.choice(clients),
                    warehouse=warehouse,
                    subtotal_paisa=total,
                    tax_paisa=0,
                    total_paisa=total,
                    posted_at=dt.datetime.combine(day, dt.time(12, 0), dt.UTC),
                )
            )
        SalesInvoice.objects.bulk_create(rows, batch_size=BATCH)
        return list(
            SalesInvoice.objects.values_list(
                "pk", "code", "posting_date", "total_paisa", "client_id"
            )
        )

    def _ledger(self, invoices, items, accounts, warehouse, rng):
        """Two ledger rows and one stock row per invoice, balanced by construction.

        ``LedgerEntry`` and ``StockEntry`` are append-only and their managers
        refuse ``bulk_create`` for good reason (CLAUDE.md §3) — the guard is
        stepped around here with the base manager, which is the whole reason
        this command carries the warning it does.
        """
        receivable = accounts["1130"]
        sales_account = accounts["4100"]
        cogs = accounts["5100"]
        inventory = accounts["1140"]

        entries = 0
        movements = 0
        batch_entries: list[LedgerEntry] = []
        batch_stock: list[StockEntry] = []

        already = set(
            LedgerEntry.objects.filter(voucher_type="SalesInvoice").values_list(
                "voucher_id", flat=True
            )
        )
        for pk, code, day, total, client_id in invoices:
            if pk in already:
                continue
            if total <= 0:
                # A draft with no lines on it. Posting one would write a
                # zero-for-zero pair, which the ledger's own check constraint
                # refuses -- correctly: an entry is exactly one non-zero side.
                continue
            cost = int(total * 0.8)
            common = {
                "posting_date": day,
                "voucher_type": "SalesInvoice",
                "voucher_id": pk,
                "voucher_code": code,
            }
            batch_entries += [
                # Party-tagged, exactly as post_sales_invoice tags it. Without
                # this the receivable ageing and the recovery workspace read
                # nothing at all -- and a report that renders in 7ms because it
                # matched no rows has not been profiled, it has been skipped.
                LedgerEntry(
                    account=receivable,
                    debit_paisa=total,
                    credit_paisa=0,
                    party_type=PartyType.CLIENT,
                    party_id=client_id,
                    **common,
                ),
                LedgerEntry(account=sales_account, debit_paisa=0, credit_paisa=total, **common),
                LedgerEntry(account=cogs, debit_paisa=cost, credit_paisa=0, **common),
                LedgerEntry(account=inventory, debit_paisa=0, credit_paisa=cost, **common),
            ]
            batch_stock.append(
                StockEntry(
                    posting_date=day,
                    item_id=rng.choice(items),
                    warehouse=warehouse,
                    qty_base=-rng.randrange(1, 50),
                    rate_paisa=100,
                    value_paisa=-cost,
                    **{k: v for k, v in common.items() if k != "posting_date"},
                )
            )

            if len(batch_entries) >= BATCH:
                entries += self._flush(LedgerEntry, batch_entries)
                movements += self._flush(StockEntry, batch_stock)

        entries += self._flush(LedgerEntry, batch_entries)
        movements += self._flush(StockEntry, batch_stock)
        return entries, movements

    def _receipts(self, invoices, accounts, rng, fraction):
        """Settle most of the year, because a real business collects its money.

        Without this the recovery workspace has 50,000 permanently open items
        and measures a business that has never been paid — which is a number
        worth knowing but is not the number the screen shows.

        A receipt against the client, exactly balancing the invoice: debit cash,
        credit receivable, party-tagged so `_split` can net the two.
        """
        if fraction <= 0:
            return 0

        cash = accounts["1110"]
        receivable = accounts["1130"]
        settled = 0
        batch: list[LedgerEntry] = []

        for pk, _code, day, total, client_id in invoices:
            if total <= 0 or rng.random() > fraction:
                continue
            paid_on = day + dt.timedelta(days=rng.randrange(1, 40))
            common = {
                "posting_date": paid_on,
                "voucher_type": "Payment",
                "voucher_id": 1_000_000 + pk,
                "voucher_code": f"RC-2026-{pk:06d}",
            }
            batch += [
                LedgerEntry(account=cash, debit_paisa=total, credit_paisa=0, **common),
                LedgerEntry(
                    account=receivable,
                    debit_paisa=0,
                    credit_paisa=total,
                    party_type=PartyType.CLIENT,
                    party_id=client_id,
                    **common,
                ),
            ]
            settled += 1
            if len(batch) >= BATCH:
                self._flush(LedgerEntry, batch)

        self._flush(LedgerEntry, batch)
        return settled

    @staticmethod
    def _flush(model, rows) -> int:
        if not rows:
            return 0
        with transaction.atomic():
            # `_base_manager` deliberately: the default manager refuses
            # bulk_create on an append-only model, and that refusal is correct
            # everywhere except in this development-only seeder.
            model._base_manager.bulk_create(rows, batch_size=BATCH)
        count = len(rows)
        rows.clear()
        return count
