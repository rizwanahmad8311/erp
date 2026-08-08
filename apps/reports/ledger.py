"""The aggregation every report is built from. **Reads only.**

Nothing in this module writes anything, and nothing in it reads a document
header. CLAUDE.md §6 in one file: every figure this app prints comes out of
:class:`~apps.accounting.models.LedgerEntry` or
:class:`~apps.accounting.models.StockEntry`, and the handful of facts those two
tables genuinely do not know — a due date, which seller booked a bill, what an
item sold *for* — are fetched from the document that owns them and joined on
here, never summed off it.

Six primitives, and every report in :mod:`apps.reports.catalog` is a
presentation of one or two of them:

    account_totals      (debit, credit) per account, over a window or to a date
    party_totals        the same, per client or vendor
    voucher_totals      the same, per voucher — what a day book is
    stock_positions     (qty, value) per (item, warehouse)
    last_movement       when each item last moved
    voucher_targets     the document each row links to, batched

What cancelled looks like from here
-----------------------------------
A cancelled document leaves **both** its original rows and their mirrors in the
ledger, and they net to zero (CLAUDE.md §3). So a *figure* is right whether or
not they are included — which is a property worth knowing rather than a licence
to ignore the toggle, because a *listing* is not right either way. A statement
showing an invoice and the line that took it back is the audit trail somebody is
asking for; a statement showing them by default is a statement with twice as
many lines as the shop has bills.

So: :func:`live` drops a row that is a reversal **and** the row it reverses, and
that is the default. ``?include_cancelled=1`` brings both back.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass

from django.apps import apps as django_apps
from django.db.models import Exists, Max, OuterRef, Q, Sum, Value
from django.db.models.functions import Coalesce

from apps.accounting.enums import PartyType, account_sign, party_sign
from apps.accounting.models import Account, LedgerEntry, StockEntry

#: Every voucher type that can appear in the ledger, and the model it resolves
#: to. A register rather than an import, for the same reason
#: :data:`apps.payments.models.ALLOCATABLE_DOCUMENTS` is one: the ledger's link
#: to its document is soft (a type name and an id, no foreign key), and a type
#: that is not listed here renders as plain text rather than as a link nothing
#: can resolve.
VOUCHER_MODELS: dict[str, tuple[str, str]] = {
    "SalesInvoice": ("sales", "SalesInvoice"),
    "SalesReturn": ("sales", "SalesReturn"),
    "PurchaseInvoice": ("purchasing", "PurchaseInvoice"),
    "PurchaseReturn": ("purchasing", "PurchaseReturn"),
    "Payment": ("payments", "Payment"),
    "ChequeEvent": ("payments", "ChequeEvent"),
}

#: What each voucher type is called on a report, so a day book does not read as
#: a column of class names.
VOUCHER_LABELS: dict[str, str] = {
    "SalesInvoice": "Sales invoice",
    "SalesReturn": "Credit note",
    "PurchaseInvoice": "Purchase invoice",
    "PurchaseReturn": "Purchase return",
    "Payment": "Receipt / payment",
    "ChequeEvent": "Cheque event",
}

#: The voucher types that put sales on a client's account.
SALES_VOUCHERS = ("SalesInvoice", "SalesReturn")
#: The voucher types that put purchases on a vendor's account.
PURCHASE_VOUCHERS = ("PurchaseInvoice", "PurchaseReturn")

#: The voucher types that move money **off** a party's account — what every
#: report in this app means by "recovery". Both of them, always, and the second
#: one is the whole subtlety:
#:
#: A bounced cheque does not reverse its payment. The payment stays POSTED
#: because it is a true record that a cheque was taken on a day (CLAUDE.md §5),
#: and the bank sending it back is a separate posting under its own
#: :class:`~apps.payments.models.ChequeEvent` — which debits the receivable
#: straight back. So a recovery figure summed over ``Payment`` alone counts
#: money that never arrived, and one summed over both is net of it, in the
#: period the bank actually sent it back.
#:
#: A *clearing* event contributes nothing here and needs no special case: it
#: moves Cheques in Hand to Bank and touches no party account, so it has no
#: party-tagged row for a party-grouped query to find.
RECOVERY_VOUCHERS = ("Payment", "ChequeEvent")


def voucher_label(voucher_type: str) -> str:
    return VOUCHER_LABELS.get(voucher_type, voucher_type)


# ===========================================================================
# What is included
# ===========================================================================
def live(queryset):
    """Drop reversals and the rows they reverse. The default for every report.

    An ``EXISTS`` rather than a join: ``reverses`` is unique-constrained so a
    join could not actually multiply a row, but an aggregate that depends on a
    uniqueness constraint elsewhere in the schema for its correctness is an
    aggregate that breaks quietly the day the constraint is relaxed.
    """
    model = queryset.model
    reversal = model.objects.filter(reverses_id=OuterRef("pk"))
    return queryset.filter(is_reversal=False).exclude(Exists(reversal))


def for_report(queryset, *, include_cancelled: bool = False):
    """``live()`` unless the audit toggle was ticked. See the module docstring."""
    return queryset if include_cancelled else live(queryset)


# ===========================================================================
# The general ledger
# ===========================================================================
def entries(
    *,
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
    account_ids=None,
    party_type: str | None = None,
    party_ids=None,
    voucher_types=None,
    include_cancelled: bool = False,
):
    """The ledger, narrowed. The one place a report builds this queryset.

    ``date_to`` is inclusive, and both dates are compared against
    ``posting_date`` — the day a document hit the books, not the day the row was
    written. Those differ whenever anything is back-dated, and only one of them
    is the answer to "what did we owe on the 30th".
    """
    queryset = for_report(LedgerEntry.objects.all(), include_cancelled=include_cancelled)
    if date_from is not None:
        queryset = queryset.filter(posting_date__gte=date_from)
    if date_to is not None:
        queryset = queryset.filter(posting_date__lte=date_to)
    if account_ids is not None:
        queryset = queryset.filter(account_id__in=list(account_ids))
    if party_type is not None:
        queryset = queryset.filter(party_type=party_type)
    if party_ids is not None:
        queryset = queryset.filter(party_id__in=list(party_ids))
    if voucher_types is not None:
        queryset = queryset.filter(voucher_type__in=list(voucher_types))
    return queryset


@dataclass(frozen=True, slots=True)
class Totals:
    """A debit total and a credit total, and the two ways to read them."""

    debit_paisa: int = 0
    credit_paisa: int = 0

    @property
    def net_paisa(self) -> int:
        """``debit - credit``. Raw: it carries no account or party sign."""
        return self.debit_paisa - self.credit_paisa

    def signed(self, sign: int) -> int:
        """The net in a natural sign — see :func:`apps.accounting.enums.account_sign`."""
        return sign * self.net_paisa

    def __add__(self, other: Totals) -> Totals:
        return Totals(
            self.debit_paisa + other.debit_paisa,
            self.credit_paisa + other.credit_paisa,
        )

    def __bool__(self) -> bool:
        return bool(self.debit_paisa or self.credit_paisa)


def grouped_totals(queryset, *keys) -> dict:
    """``{key: Totals}`` for a ledger queryset grouped by one or more columns.

    One key gives a plain key; several give a tuple, in the order named. The
    escape hatch for a report that needs a grouping the four named functions
    below do not cover — a route day sheet wants ``(party, voucher type)``, and
    inventing a function per grouping would be five lines of ``GROUP BY`` behind
    five names nobody can keep straight.
    """
    rows = (
        queryset.order_by()
        .values(*keys)
        .annotate(
            debit=Coalesce(Sum("debit_paisa"), Value(0)),
            credit=Coalesce(Sum("credit_paisa"), Value(0)),
        )
    )
    result = {}
    for row in rows:
        key = row[keys[0]] if len(keys) == 1 else tuple(row[key] for key in keys)
        result[key] = Totals(row["debit"], row["credit"])
    return result


def account_totals(**kwargs) -> dict[int, Totals]:
    """``{account_id: Totals}`` over whatever window was asked for.

    With no ``date_from`` this is cumulative — which is what a trial balance and
    a balance sheet want. With both dates it is movement in a period, which is
    what a profit and loss wants. One function, because the difference between
    those two reports is genuinely nothing more than that.
    """
    return grouped_totals(entries(**kwargs), "account_id")


def party_totals(party_type: str, **kwargs) -> dict[int, Totals]:
    """``{party_id: Totals}`` for one side of the business."""
    return grouped_totals(entries(party_type=party_type, **kwargs), "party_id")


def voucher_totals(**kwargs) -> dict[tuple[str, int], Totals]:
    """``{(voucher_type, voucher_id): Totals}`` — the day book's primitive."""
    return grouped_totals(entries(**kwargs), "voucher_type", "voucher_id")


def account_balance_paisa(account: Account, totals: dict[int, Totals]) -> int:
    """One account's balance, in its natural sign, from a totals map.

    A group totals its whole subtree and a leaf is its own subtree, so a caller
    never branches on ``is_group`` — the same contract
    :func:`apps.accounting.services.account_balance` offers, answered from
    figures already in hand rather than with another query per account.
    """
    net = sum(totals.get(pk, Totals()).net_paisa for pk in account.subtree_ids())
    return account_sign(account.type) * net


def party_balance_paisa(party_type: str, party_id: int, totals: dict[int, Totals]) -> int:
    """One party's balance in its natural sign, from a totals map."""
    return party_sign(party_type) * totals.get(party_id, Totals()).net_paisa


def parties_with_movement(party_type: str, **kwargs) -> list[int]:
    """The ids of every party the ledger has ever heard of, for this side.

    Cheaper than listing every client and asking about each: a shop that has
    never traded has no rows and belongs on no statement.
    """
    return list(
        entries(party_type=party_type, **kwargs)
        .order_by()
        .values_list("party_id", flat=True)
        .distinct()
    )


# ===========================================================================
# The stock ledger
# ===========================================================================
def stock_entries(
    *,
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
    item_ids=None,
    warehouse_ids=None,
    voucher_types=None,
    include_cancelled: bool = False,
):
    """The stock ledger, narrowed. The stock twin of :func:`entries`."""
    queryset = for_report(StockEntry.objects.all(), include_cancelled=include_cancelled)
    if date_from is not None:
        queryset = queryset.filter(posting_date__gte=date_from)
    if date_to is not None:
        queryset = queryset.filter(posting_date__lte=date_to)
    if item_ids is not None:
        queryset = queryset.filter(item_id__in=list(item_ids))
    if warehouse_ids is not None:
        queryset = queryset.filter(warehouse_id__in=list(warehouse_ids))
    if voucher_types is not None:
        queryset = queryset.filter(voucher_type__in=list(voucher_types))
    return queryset


@dataclass(frozen=True, slots=True)
class StockTotals:
    """Base units and the cost behind them. Signed: positive in, negative out."""

    qty_base: int = 0
    value_paisa: int = 0

    def __add__(self, other: StockTotals) -> StockTotals:
        return StockTotals(self.qty_base + other.qty_base, self.value_paisa + other.value_paisa)

    @property
    def rate_paisa(self) -> int:
        """The weighted average behind this position, or 0 when it holds nothing.

        Floor division rather than :func:`~apps.core.money.round_paisa`: this is
        a derived display figure on a stock report, not a value anything is
        posted from. The figure that counts is ``value_paisa``
        (:class:`apps.accounting.models.StockEntry`).
        """
        return self.value_paisa // self.qty_base if self.qty_base else 0


def stock_positions(**kwargs) -> dict[tuple[int, int], StockTotals]:
    """``{(item_id, warehouse_id): StockTotals}``.

    With no ``date_from`` this is the position as at ``date_to`` — the stock
    balance report. With both dates it is the movement in a period, which is
    what an item-wise sales summary counts.
    """
    rows = (
        stock_entries(**kwargs)
        .order_by()
        .values("item_id", "warehouse_id")
        .annotate(
            qty=Coalesce(Sum("qty_base"), Value(0)), value=Coalesce(Sum("value_paisa"), Value(0))
        )
    )
    return {
        (row["item_id"], row["warehouse_id"]): StockTotals(row["qty"], row["value"]) for row in rows
    }


def stock_by_item(**kwargs) -> dict[int, StockTotals]:
    """``{item_id: StockTotals}`` — every warehouse folded together.

    The quantity is meaningful across warehouses and so is the value; the *rate*
    is not, which is why :func:`apps.accounting.services.valuation_rate` insists
    on one warehouse and this returns no rate of its own.
    """
    rows = (
        stock_entries(**kwargs)
        .order_by()
        .values("item_id")
        .annotate(
            qty=Coalesce(Sum("qty_base"), Value(0)), value=Coalesce(Sum("value_paisa"), Value(0))
        )
    )
    return {row["item_id"]: StockTotals(row["qty"], row["value"]) for row in rows}


def last_movement(
    *, as_of: dt.date, item_ids=None, include_cancelled: bool = False
) -> dict[int, dt.date]:
    """``{item_id: the last day it moved}``, up to ``as_of``.

    What "slow moving" is measured against. An item with no entry at all is
    absent from the map rather than dated far in the past, because "has never
    moved" and "has not moved lately" are different answers to the buyer.
    """
    rows = (
        stock_entries(date_to=as_of, item_ids=item_ids, include_cancelled=include_cancelled)
        .order_by()
        .values("item_id")
        .annotate(last=Max("posting_date"))
    )
    return {row["item_id"]: row["last"] for row in rows}


# ===========================================================================
# Drilling through to the document
# ===========================================================================
@dataclass(frozen=True, slots=True)
class VoucherTarget:
    """Where a report row points, and what state that document is in."""

    url: str = ""
    status: str = ""
    code: str = ""


def voucher_targets(pairs) -> dict[tuple[str, int], VoucherTarget]:
    """``{(voucher_type, voucher_id): VoucherTarget}``, one query per type.

    This is what makes "every report row that references a document links to
    that document" cheap enough to be unconditional. A page of two hundred
    ledger rows touches at most six document tables, so it costs six queries —
    not two hundred, which is what a ``get_absolute_url()`` per row would cost
    and is why reports usually end up without links at all.

    A type this system does not know is skipped rather than raised on: the
    ledger outlives its documents by design, and a row naming a model that has
    since been removed must still print.
    """
    wanted: dict[str, set[int]] = defaultdict(set)
    for voucher_type, voucher_id in pairs:
        if voucher_type in VOUCHER_MODELS:
            wanted[voucher_type].add(voucher_id)

    targets: dict[tuple[str, int], VoucherTarget] = {}
    for voucher_type, ids in wanted.items():
        app_label, model_name = VOUCHER_MODELS[voucher_type]
        try:
            model = django_apps.get_model(app_label, model_name)
        except LookupError:  # pragma: no cover - only if an app is removed
            continue
        for document in model.objects.filter(pk__in=ids):
            targets[(voucher_type, document.pk)] = VoucherTarget(
                url=document.get_absolute_url(),
                status=str(getattr(document, "status", "")),
                code=str(getattr(document, "code", "")),
            )
    return targets


def documents_in_period(
    model, *, date_from, date_to, include_cancelled=False, live=False, **filters
):
    """Posted documents of one type in a window, for the dimension they carry.

    The **only** thing a report is allowed to read off a document is a fact the
    ledger does not hold: which seller booked it, which route it belongs to,
    what the goods sold for. The money still comes from
    :func:`voucher_totals`, keyed by the ids this returns.

    ``POSTED`` only, because a draft has written nothing to any ledger and
    counting it would report a bill that does not exist yet.

    ``live=True`` is for :class:`~apps.payments.models.Payment`, whose ``live()``
    is narrower than "not cancelled": it also drops a cheque that bounced. A
    bounce does **not** reverse the payment — it is a separate posting under its
    own :class:`~apps.payments.models.ChequeEvent` voucher, and the payment
    stays POSTED because it is a true record that a cheque was taken that day.
    So a recovery figure that counted the payment's own rows would count money
    that never arrived, and this is where that is prevented.

    With the audit toggle on, cancelled documents come back and the caller is
    expected to say on screen that the figures no longer agree with the ledger.
    """
    queryset = model.objects.filter(posting_date__gte=date_from, posting_date__lte=date_to)
    if include_cancelled:
        queryset = queryset.filter(Q(status="POSTED") | Q(status="CANCELLED"))
    elif live:
        queryset = queryset.live()
    else:
        queryset = queryset.posted()
    return queryset.filter(**filters) if filters else queryset


__all__ = [
    "PURCHASE_VOUCHERS",
    "RECOVERY_VOUCHERS",
    "SALES_VOUCHERS",
    "VOUCHER_LABELS",
    "VOUCHER_MODELS",
    "PartyType",
    "StockTotals",
    "Totals",
    "VoucherTarget",
    "account_balance_paisa",
    "account_totals",
    "documents_in_period",
    "entries",
    "for_report",
    "grouped_totals",
    "last_movement",
    "live",
    "parties_with_movement",
    "party_balance_paisa",
    "party_totals",
    "stock_by_item",
    "stock_entries",
    "stock_positions",
    "voucher_label",
    "voucher_targets",
    "voucher_totals",
]
