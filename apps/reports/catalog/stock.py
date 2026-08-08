"""The stock reports. Every quantity and every rupee of value is the stock ledger.

There is no ``stock_on_hand`` field anywhere in this system and there must not be
(CLAUDE.md §6) — what is held, and what it cost, is
:class:`~apps.accounting.models.StockEntry` summed up, every time, and these four
reports are four ways of summing it.

One of them needs a figure the stock ledger genuinely does not hold. The stock
ledger records what goods **cost**; what they **sold for** is on the sales line
and nowhere else, because a general ledger has no item dimension and never will.
So the item-wise summary takes its quantity and its cost from the stock ledger,
and joins the revenue on from the lines of the documents the ledger already
counted — see :func:`_item_movement`. That join is stated on the report itself,
because a margin figure whose two halves come from two places is a figure people
should be able to see the seams of.
"""

from __future__ import annotations

import datetime as dt

from django.db.models import Sum, Value
from django.db.models.functions import Coalesce

from apps.accounting.models import Warehouse
from apps.core.money import fmt
from apps.masters.models import Item
from apps.masters.services import fmt_qty
from apps.purchasing.models import (
    PurchaseInvoice,
    PurchaseInvoiceLine,
    PurchaseReturn,
    PurchaseReturnLine,
)
from apps.sales.models import SalesInvoice, SalesInvoiceLine, SalesReturn, SalesReturnLine

from .. import ledger
from ..columns import CODE, COUNT, DATE, MONEY, QTY, TEXT, Column, ReportRow, total_row
from ..registry import Report, ReportResult, register

GROUP = "Stock"


# ===========================================================================
# Stock balance
# ===========================================================================
STOCK_BALANCE_COLUMNS = (
    Column("code", "Item code", CODE, width=12),
    Column("item", "Item", TEXT, width=30),
    Column("warehouse", "Warehouse", TEXT, width=14),
    Column("qty", "On hand", QTY, width=13),
    Column("pieces", "Pieces", QTY, width=9),
    Column("rate", "Rate", MONEY, width=10, sensitive=True),
    Column("value", "Value", MONEY, width=12, total=True, sensitive=True),
)


def _build_stock_balance(criteria) -> ReportResult:
    """What is held in each warehouse, and what it is worth, as at a date.

    Quantity is shown twice on purpose: once as a picker reads it
    (``"3 ctn + 5 pcs"``) and once as the base units the ledger actually stores.
    The two are the same number — :func:`apps.masters.services.fmt_qty` is a
    display conversion, never a stored one (CLAUDE.md §2) — and printing both is
    what stops a warehouseman and an accountant arguing about a figure they are
    each reading correctly.
    """
    positions = ledger.stock_positions(
        date_to=criteria.as_of,
        item_ids=[criteria.item.pk] if criteria.item else None,
        warehouse_ids=[criteria.warehouse.pk] if criteria.warehouse else None,
        include_cancelled=criteria.include_cancelled,
    )
    items = {item.pk: item for item in Item.objects.filter(pk__in={key[0] for key in positions})}
    warehouses = {
        warehouse.pk: warehouse
        for warehouse in Warehouse.objects.filter(pk__in={key[1] for key in positions})
    }

    rows = []
    for (item_id, warehouse_id), position in positions.items():
        item = items.get(item_id)
        warehouse = warehouses.get(warehouse_id)
        if item is None or warehouse is None:  # pragma: no cover - PROTECTed both ways
            continue
        if position.qty_base == 0 and position.value_paisa == 0:
            continue
        rows.append(
            ReportRow(
                values={
                    "code": item.code,
                    "item": item.name,
                    "warehouse": warehouse.code,
                    "qty": fmt_qty(item, position.qty_base),
                    "pieces": position.qty_base,
                    "rate": position.rate_paisa,
                    "value": position.value_paisa,
                },
                # A negative position has no cost behind it to average, so every
                # later issue out of it is valued at a guess. It is the one thing
                # on this page somebody has to fix.
                alarm=frozenset({"qty", "pieces"}) if position.qty_base < 0 else frozenset(),
            )
        )

    rows.sort(key=lambda row: (str(row.get("code")), str(row.get("warehouse"))))
    negative = sum(1 for row in rows if int(row.get("pieces")) < 0)

    notes = [
        "Value is the stock ledger's own moving weighted average, per (item, warehouse). "
        "It is not quantity times a price list.",
    ]
    if negative:
        notes.append(
            f"{negative} position{'s are' if negative != 1 else ' is'} negative. A negative "
            f"balance has no cost behind it, so anything issued out of it is valued at the "
            f"last rate known — correct the receipt that is missing."
        )

    return ReportResult(
        rows=rows,
        totals={"item": "Total", "value": sum(int(row.get("value")) for row in rows)},
        subtitle=f"As at {criteria.as_of:%d %b %Y}",
        notes=tuple(notes),
    )


register(
    Report(
        slug="stock-balance",
        title="Stock Balance",
        group=GROUP,
        description="What is held per item and warehouse, in quantity and in value.",
        columns=STOCK_BALANCE_COLUMNS,
        filters=("as_of", "item", "warehouse"),
        build=_build_stock_balance,
    )
)


# ===========================================================================
# Stock ledger
# ===========================================================================
STOCK_LEDGER_COLUMNS = (
    Column("date", "Date", DATE, width=10),
    Column("voucher", "Voucher", CODE, width=15, link=True),
    Column("type", "Type", TEXT, width=14),
    Column("warehouse", "Warehouse", TEXT, width=10),
    Column("in_qty", "In", QTY, width=9, blank_zero=True),
    Column("out_qty", "Out", QTY, width=9, blank_zero=True),
    Column("rate", "Rate", MONEY, width=10, sensitive=True),
    Column("value", "Value", MONEY, width=11, total=True, sensitive=True),
    Column("balance_qty", "Balance", QTY, width=10),
    Column("balance_value", "Balance value", MONEY, width=12, sensitive=True),
)


def _build_stock_ledger(criteria) -> ReportResult:
    """One item's movement history, with a running position.

    The opening row is the position as at the day before the window, taken from
    the same aggregation the balance report uses, so the card can be started at
    any date without replaying the whole history on screen.

    ``rate`` is the rate **stored on the row** — what the goods cost on the way
    in, and the moving weighted average on the way out as it stood at that
    moment. It is not recomputed here, which is what lets a stock card be read
    back years later and still say what it said at the time.
    """
    item = criteria.item
    warehouse_ids = [criteria.warehouse.pk] if criteria.warehouse else None

    opening = ledger.stock_by_item(
        date_to=criteria.day_before,
        item_ids=[item.pk],
        warehouse_ids=warehouse_ids,
        include_cancelled=criteria.include_cancelled,
    ).get(item.pk, ledger.StockTotals())

    entries = list(
        ledger.stock_entries(
            date_from=criteria.date_from,
            date_to=criteria.date_to,
            item_ids=[item.pk],
            warehouse_ids=warehouse_ids,
            include_cancelled=criteria.include_cancelled,
        )
        .select_related("warehouse")
        .order_by("posting_date", "id")
    )
    targets = ledger.voucher_targets((entry.voucher_type, entry.voucher_id) for entry in entries)

    running = opening
    rows = [
        ReportRow(
            values={
                "type": "Opening balance",
                "balance_qty": fmt_qty(item, opening.qty_base),
                "balance_value": opening.value_paisa,
            },
            emphasis="opening",
        )
    ]
    for entry in entries:
        running = running + ledger.StockTotals(entry.qty_base, entry.value_paisa)
        target = targets.get((entry.voucher_type, entry.voucher_id), ledger.VoucherTarget())
        rows.append(
            ReportRow(
                values={
                    "date": entry.posting_date,
                    "voucher": entry.voucher_code,
                    "type": ledger.voucher_label(entry.voucher_type),
                    "warehouse": entry.warehouse.code,
                    "in_qty": entry.qty_base if entry.qty_base > 0 else 0,
                    "out_qty": -entry.qty_base if entry.qty_base < 0 else 0,
                    "rate": entry.rate_paisa,
                    "value": entry.value_paisa,
                    "balance_qty": fmt_qty(item, running.qty_base),
                    "balance_value": running.value_paisa,
                },
                url=target.url,
                status=target.status,
            )
        )

    where = f" in {criteria.warehouse.code}" if criteria.warehouse else " across every warehouse"
    return ReportResult(
        rows=rows,
        totals={
            "type": "Closing balance",
            "value": running.value_paisa - opening.value_paisa,
            "balance_qty": fmt_qty(item, running.qty_base),
            "balance_value": running.value_paisa,
        },
        subtitle=f"{item.code} — {item.name}{where} · {criteria.period_label}",
        notes=(
            "Quantities are base units. Rate is the cost stored on the row when it was "
            "written, never a rate recomputed today.",
            f"Closing position {fmt_qty(item, running.qty_base)} at {fmt(running.value_paisa)}.",
        ),
    )


register(
    Report(
        slug="stock-ledger",
        title="Stock Ledger",
        group=GROUP,
        description="One item's movement history, with a running quantity and value.",
        columns=STOCK_LEDGER_COLUMNS,
        filters=("item", "warehouse", "date_from", "date_to"),
        requires=("item",),
        landscape=True,
        build=_build_stock_ledger,
    )
)


# ===========================================================================
# Item-wise sales and purchase
# ===========================================================================
def _line_amounts(line_model, document_ids) -> dict[int, tuple[int, int]]:
    """``{item_id: (net_paisa, tax_paisa)}`` for a set of documents' lines.

    The **only** thing read off a document in this module, and it is read off a
    *line*, not a header total: what an item sold for exists nowhere else in the
    system. The set of documents is decided by the ledger first — see
    :func:`_item_movement` — so a cancelled bill cannot get in here through the
    back door.
    """
    rows = (
        line_model.objects.filter(document_id__in=document_ids)
        .order_by()
        .values("item_id")
        .annotate(
            net=Coalesce(Sum("amount_paisa"), Value(0)),
            tax=Coalesce(Sum("tax_paisa"), Value(0)),
        )
    )
    return {row["item_id"]: (row["net"], row["tax"]) for row in rows}


def _item_movement(criteria, *, models, sign: int):
    """Quantity and cost from the stock ledger, revenue from the lines.

    ``models`` is ``[(document model, line model, voucher type, direction)]`` —
    an invoice and its credit note, or a purchase and its return. ``sign`` flips
    the stock ledger's convention (positive in, negative out) so that "sold" and
    "bought" both read as positive numbers on the page.
    """
    totals: dict[int, dict[str, int]] = {}

    for document_model, line_model, voucher_type, direction in models:
        documents = ledger.documents_in_period(
            document_model,
            date_from=criteria.date_from,
            date_to=criteria.date_to,
            include_cancelled=criteria.include_cancelled,
        )
        document_ids = list(documents.values_list("pk", flat=True))
        if not document_ids:
            continue

        moved = (
            ledger.stock_entries(
                date_from=criteria.date_from,
                date_to=criteria.date_to,
                voucher_types=[voucher_type],
                include_cancelled=criteria.include_cancelled,
            )
            .filter(voucher_id__in=document_ids)
            .order_by()
            .values("item_id")
            .annotate(
                qty=Coalesce(Sum("qty_base"), Value(0)),
                value=Coalesce(Sum("value_paisa"), Value(0)),
            )
        )
        for row in moved:
            bucket = totals.setdefault(row["item_id"], {"qty": 0, "cost": 0, "net": 0, "tax": 0})
            bucket["qty"] += sign * direction * row["qty"]
            bucket["cost"] += sign * direction * row["value"]

        for item_id, (net, tax) in _line_amounts(line_model, document_ids).items():
            bucket = totals.setdefault(item_id, {"qty": 0, "cost": 0, "net": 0, "tax": 0})
            bucket["net"] += direction * net
            bucket["tax"] += direction * tax

    return totals


ITEM_SALES_COLUMNS = (
    Column("code", "Item code", CODE, width=12),
    Column("item", "Item", TEXT, width=28),
    Column("qty", "Sold", QTY, width=12),
    Column("pieces", "Pieces", QTY, width=9),
    Column("revenue", "Revenue", MONEY, width=13, total=True),
    Column("tax", "Tax", MONEY, width=11, total=True, blank_zero=True),
    Column("cost", "Cost", MONEY, width=12, total=True, sensitive=True),
    Column("margin", "Margin", MONEY, width=12, total=True, sensitive=True),
)


def _build_item_sales(criteria) -> ReportResult:
    movement = _item_movement(
        criteria,
        models=[
            (SalesInvoice, SalesInvoiceLine, "SalesInvoice", 1),
            (SalesReturn, SalesReturnLine, "SalesReturn", -1),
        ],
        # Stock goes *out* on a sale, so the ledger's quantity is negative and
        # the sign flips to make "sold 120" read as 120.
        sign=-1,
    )
    items = {item.pk: item for item in Item.objects.filter(pk__in=movement)}

    rows = []
    for item_id, figures in movement.items():
        item = items.get(item_id)
        if item is None or not any(figures.values()):
            continue
        margin = figures["net"] - figures["cost"]
        rows.append(
            ReportRow(
                values={
                    "code": item.code,
                    "item": item.name,
                    "qty": fmt_qty(item, figures["qty"]),
                    "pieces": figures["qty"],
                    "revenue": figures["net"],
                    "tax": figures["tax"],
                    "cost": figures["cost"],
                    "margin": margin,
                },
                alarm=frozenset({"margin"}) if margin < 0 else frozenset(),
            )
        )

    rows.sort(key=lambda row: -int(row.get("revenue") or 0))
    return ReportResult(
        rows=rows,
        totals=total_row(ITEM_SALES_COLUMNS, rows, label_key="item"),
        subtitle=criteria.period_label,
        notes=(
            "Quantity and cost are the stock ledger's. Revenue is the sales lines' own net "
            "amount — the general ledger has no item dimension, so it is the only place the "
            "selling price exists.",
            "Credit notes are netted off, both in quantity and in value.",
        ),
    )


register(
    Report(
        slug="item-sales",
        title="Item-wise Sales",
        group=GROUP,
        description="What each item sold, what it cost, and the margin between them.",
        columns=ITEM_SALES_COLUMNS,
        filters=("date_from", "date_to"),
        landscape=True,
        build=_build_item_sales,
    )
)


ITEM_PURCHASE_COLUMNS = (
    Column("code", "Item code", CODE, width=12),
    Column("item", "Item", TEXT, width=32),
    Column("qty", "Received", QTY, width=13),
    Column("pieces", "Pieces", QTY, width=10),
    Column("cost", "Cost", MONEY, width=14, total=True, sensitive=True),
    Column("billed", "Billed", MONEY, width=14, total=True, sensitive=True),
)


def _build_item_purchases(criteria) -> ReportResult:
    movement = _item_movement(
        criteria,
        models=[
            (PurchaseInvoice, PurchaseInvoiceLine, "PurchaseInvoice", 1),
            (PurchaseReturn, PurchaseReturnLine, "PurchaseReturn", -1),
        ],
        # Stock comes *in* on a purchase, so the ledger's sign is already right.
        sign=1,
    )
    items = {item.pk: item for item in Item.objects.filter(pk__in=movement)}

    rows = []
    for item_id, figures in movement.items():
        item = items.get(item_id)
        if item is None or not any(figures.values()):
            continue
        rows.append(
            ReportRow(
                values={
                    "code": item.code,
                    "item": item.name,
                    "qty": fmt_qty(item, figures["qty"]),
                    "pieces": figures["qty"],
                    "cost": figures["cost"],
                    "billed": figures["net"],
                }
            )
        )

    rows.sort(key=lambda row: -int(row.get("cost") or 0))
    return ReportResult(
        rows=rows,
        totals=total_row(ITEM_PURCHASE_COLUMNS, rows, label_key="item"),
        subtitle=criteria.period_label,
        notes=(
            "Cost is what the stock ledger took the goods in at. Billed is what the supplier "
            "charged on the line. They differ when a bill carries freight or a discount that "
            "was spread across the lines.",
        ),
    )


register(
    Report(
        slug="item-purchases",
        title="Item-wise Purchases",
        group=GROUP,
        description="What each item was received at, against what the supplier billed.",
        columns=ITEM_PURCHASE_COLUMNS,
        filters=("date_from", "date_to"),
        build=_build_item_purchases,
    )
)


# ===========================================================================
# Slow moving
# ===========================================================================
SLOW_MOVING_COLUMNS = (
    Column("code", "Item code", CODE, width=12),
    Column("item", "Item", TEXT, width=32),
    Column("qty", "On hand", QTY, width=13),
    Column("value", "Value tied up", MONEY, width=14, total=True, sensitive=True),
    Column("last_moved", "Last moved", DATE, width=12),
    Column("idle_days", "Idle days", COUNT, width=10),
)


def _build_slow_moving(criteria) -> ReportResult:
    """Stock that is sitting there. Money on a shelf, listed by how much of it.

    "No movement" means no stock entry at all — not "no sale". An item that was
    received last week and has not sold is not slow moving, it is new, and a
    report that could not tell the difference would have the buyer writing off a
    line they had just bought.

    Items that have **never** moved are excluded rather than reported as
    infinitely idle: with no entry there is no position either, so there is
    nothing sitting on a shelf to worry about.
    """
    cutoff = criteria.as_of - dt.timedelta(days=criteria.days)

    positions = ledger.stock_by_item(
        date_to=criteria.as_of, include_cancelled=criteria.include_cancelled
    )
    held = {item_id: total for item_id, total in positions.items() if total.qty_base > 0}
    if not held:
        return ReportResult(subtitle=f"As at {criteria.as_of:%d %b %Y}")

    moved = ledger.last_movement(
        as_of=criteria.as_of, item_ids=list(held), include_cancelled=criteria.include_cancelled
    )
    items = {item.pk: item for item in Item.objects.filter(pk__in=held)}

    rows = []
    for item_id, position in held.items():
        last = moved.get(item_id)
        item = items.get(item_id)
        if item is None or last is None or last > cutoff:
            continue
        rows.append(
            ReportRow(
                values={
                    "code": item.code,
                    "item": item.name,
                    "qty": fmt_qty(item, position.qty_base),
                    "value": position.value_paisa,
                    "last_moved": last,
                    "idle_days": (criteria.as_of - last).days,
                }
            )
        )

    rows.sort(key=lambda row: -int(row.get("value") or 0))
    return ReportResult(
        rows=rows,
        totals=total_row(SLOW_MOVING_COLUMNS, rows, label_key="item"),
        subtitle=(
            f"Nothing in or out for {criteria.days} days, as at {criteria.as_of:%d %b %Y} "
            f"(on or before {cutoff:%d %b %Y})"
        ),
        notes=(
            "Movement means any stock entry — a receipt counts as well as a sale. An item "
            "received last week is new, not slow.",
            "Items that have never moved are not listed: with no entry there is no position, "
            "so there is nothing on the shelf.",
        ),
    )


register(
    Report(
        slug="slow-moving",
        title="Slow-moving Items",
        group=GROUP,
        description="Stock that has not moved in N days, most money tied up first.",
        columns=SLOW_MOVING_COLUMNS,
        filters=("as_of", "days"),
        build=_build_slow_moving,
    )
)


__all__ = ["GROUP"]
