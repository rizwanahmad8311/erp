"""Ageing, open items and the recovery workspace. **Reads only.**

Nothing in this module writes anything. It is the aggregation layer the
accountant's screen is built from, and every figure on that screen comes from
one of two places:

* **how much** — the general ledger, grouped by the voucher that wrote it, minus
  the allocations that have been applied to it;
* **when it fell due** — the document, because a due date is a fact about a bill
  and the ledger has never heard of one.

That split is the whole of CLAUDE.md §6 as it applies here. No amount on this
screen is read off a document header. ``SalesInvoice.total_paisa`` exists and is
correct, and this module still does not touch it — because the day a cancelled
invoice, a credit note or a part payment enters the picture, the header and the
ledger stop being the same number, and only one of them is right.

The one primitive
-----------------
Everything below is built on grouping ledger rows by ``(voucher_type,
voucher_id)`` for one party::

    net = party_sign(party_type) * (sum(debit) - sum(credit))

Positive means the voucher put money on the party's account in the normal
direction of business — an invoice a client owes, a bill we owe a supplier.
Negative means it took money off — a receipt, a credit note. So:

* positive vouchers are **open items**, less whatever has been allocated to them;
* negative vouchers are **credits**, less whatever of them has been applied;
* what is left of a credit is an **on-account balance**, which is a normal state
  and must be visible, not an error.

And the two halves tie out exactly:

    sum(open items outstanding) - sum(unapplied credits) == party_balance()

which ``tests/test_recovery.py`` asserts, because it is the property that makes
the workspace trustworthy: if the ageing ladder ever stops adding up to the
client's ledger balance, one of the two is lying.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date

from django.db.models import Count, Min, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.accounting.enums import PartyType, party_sign
from apps.accounting.models import LedgerEntry
from apps.accounting.refs import VoucherRef
from apps.accounting.services import party_balance
from apps.core.enums import DocumentStatus
from apps.masters.models import Client, Route

from .enums import (
    AGEING_BUCKETS,
    OVERDUE_BUCKETS,
    AgeingBucket,
    ChequeEventKind,
    PaymentDirection,
    PaymentMode,
    bucket_for,
)
from .models import (
    ALLOCATABLE_DOCUMENTS,
    ChequeEvent,
    Payment,
    PaymentAllocation,
    allocatable_model,
    allocatable_spec,
)


def _as_of(value: date | None) -> date:
    """Today in the installation's timezone, unless a date was given.

    ``timezone.localdate()`` rather than ``date.today()``: ``USE_TZ`` is on and
    ``TIME_ZONE`` is Asia/Karachi, so at 3am UTC the two disagree about what day
    it is and the whole ladder shifts by one.
    """
    return value or timezone.localdate()


# ===========================================================================
# One document
# ===========================================================================
def document_balance_paisa(document, *, as_of: date | None = None) -> int:
    """What this document still puts on its party's account, from the ledger.

    Party-signed: positive for a bill, negative for a credit note. A cancelled
    document returns 0 without anything having to know it was cancelled — its
    original rows and their reversals are both in the sum and they net out
    (CLAUDE.md §3 paying for itself).

    Deliberately **not** ``document.total_paisa``. The header is a display
    convenience on the document that owns it and the source of truth for
    nothing (CLAUDE.md §6); this is the figure the books actually carry.
    """
    spec = allocatable_spec(type(document).__name__)
    ref = VoucherRef.of(document)

    entries = LedgerEntry.objects.filter(
        voucher_type=ref.type,
        voucher_id=ref.id,
        party_type=spec.party_type,
        party_id=getattr(document, spec.party_field),
    )
    if as_of is not None:
        entries = entries.filter(posting_date__lte=as_of)

    totals = entries.aggregate(
        debit=Coalesce(Sum("debit_paisa"), Value(0)),
        credit=Coalesce(Sum("credit_paisa"), Value(0)),
    )
    return party_sign(spec.party_type) * (totals["debit"] - totals["credit"])


def document_allocated_paisa(document) -> int:
    """How much live money has been applied to this document."""
    return (
        PaymentAllocation.objects.live()
        .for_document(document)
        .aggregate(
            total=Coalesce(Sum("amount_paisa"), Value(0)),
        )["total"]
    )


def document_open_paisa(document) -> int:
    """How much of this document is still unsettled, as a positive number.

    The magnitude, so a bill and a credit note answer the same question the same
    way: "how much more may be applied here". A fully settled document returns
    0, and :func:`apps.payments.services.allocate_payment` refuses anything that
    would take it below that.
    """
    return abs(document_balance_paisa(document)) - document_allocated_paisa(document)


def document_due_date(document):
    """When this document falls due, or its posting date if it never does.

    Only :class:`~apps.sales.models.SalesInvoice` carries a due date today. For
    everything else the honest answer is "on sight", and using the posting date
    is what makes a credit note or a supplier bill age from the day it hit the
    books rather than never ageing at all.
    """
    spec = allocatable_spec(type(document).__name__)
    if spec.due_date_field:
        due = getattr(document, spec.due_date_field, None)
        if due is not None:
            return due
    return document.posting_date


# ===========================================================================
# Open items
# ===========================================================================
@dataclass(frozen=True, slots=True)
class OpenItem:
    """One unsettled document on a party's account, aged.

    ``original_paisa`` is what the **ledger** says the document put on the
    account, not what its header says. ``due_date`` is the one figure here that
    comes from the document, because nothing else knows it.
    """

    voucher_type: str
    voucher_id: int
    voucher_code: str
    posting_date: date
    due_date: date
    original_paisa: int
    allocated_paisa: int
    as_of: date

    @property
    def outstanding_paisa(self) -> int:
        return self.original_paisa - self.allocated_paisa

    @property
    def days_overdue(self) -> int:
        """Days past the due date. Negative means it is not due yet."""
        return (self.as_of - self.due_date).days

    @property
    def bucket(self) -> str:
        return bucket_for(self.days_overdue)

    @property
    def bucket_label(self) -> str:
        return AgeingBucket(self.bucket).label

    @property
    def is_overdue(self) -> bool:
        return self.bucket in OVERDUE_BUCKETS

    @property
    def is_part_paid(self) -> bool:
        return self.allocated_paisa > 0

    def document(self):
        """The document itself, when a screen needs more than the code."""
        return allocatable_model(self.voucher_type).objects.filter(pk=self.voucher_id).first()


@dataclass(frozen=True, slots=True)
class Credit:
    """A voucher sitting on the account in the party's favour.

    A receipt nobody has applied to a bill yet, or a credit note. What is left
    of it is an **on-account balance** — normal, and the thing a recovery screen
    must never hide, because money that is invisible gets collected twice.
    """

    voucher_type: str
    voucher_id: int
    voucher_code: str
    posting_date: date
    original_paisa: int
    applied_paisa: int

    @property
    def unapplied_paisa(self) -> int:
        return self.original_paisa - self.applied_paisa

    @property
    def is_payment(self) -> bool:
        return self.voucher_type == Payment.__name__


@dataclass(frozen=True, slots=True)
class _VoucherRow:
    """A raw ledger group-by row, before it is split into bills and credits."""

    voucher_type: str
    voucher_id: int
    voucher_code: str
    posting_date: date
    net_paisa: int


def _voucher_rows(
    party_type: str, *, party_ids=None, as_of: date | None = None
) -> dict[int, list[_VoucherRow]]:
    """Every party's ledger, grouped by the voucher that wrote each row.

    One query for the whole screen. ``voucher_code`` joins the GROUP BY rather
    than being looked up afterwards — it is denormalised onto every ledger row
    for exactly this reason (CLAUDE.md §6), so a recovery sheet never joins out
    to a dozen document tables to print a document number.
    """
    entries = LedgerEntry.objects.filter(party_type=party_type)
    if party_ids is not None:
        entries = entries.filter(party_id__in=party_ids)
    if as_of is not None:
        entries = entries.filter(posting_date__lte=as_of)

    sign = party_sign(party_type)
    grouped = (
        entries.values("party_id", "voucher_type", "voucher_id", "voucher_code")
        .annotate(
            debit=Coalesce(Sum("debit_paisa"), Value(0)),
            credit=Coalesce(Sum("credit_paisa"), Value(0)),
            first_date=Min("posting_date"),
        )
        .order_by("first_date", "voucher_id")
    )

    raw: dict[int, dict[tuple[str, int], _VoucherRow]] = {}
    for row in grouped:
        key = (row["voucher_type"], row["voucher_id"])
        raw.setdefault(row["party_id"], {})[key] = _VoucherRow(
            voucher_type=row["voucher_type"],
            voucher_id=row["voucher_id"],
            voucher_code=row["voucher_code"],
            posting_date=row["first_date"],
            net_paisa=sign * (row["debit"] - row["credit"]),
        )

    _fold_bounces(raw)

    return {
        party_id: [row for row in by_key.values() if row.net_paisa]
        # A voucher that nets to zero is a cancelled document, or a bounced
        # cheque folded back onto the receipt it undid. Nothing left to chase
        # and nothing left to apply.
        for party_id, by_key in raw.items()
    }


def _fold_bounces(raw) -> None:
    """Merge a bounced cheque's entries back onto the receipt they undo.

    A bounce posts against its own voucher, which is right — it happened on its
    own day and it can be cancelled on its own. But on a recovery sheet the pair
    would read as two lines that cancel out: a receipt of Rs 4,000 in the shop's
    favour and a ``CHQ-…`` of Rs 4,000 against them. Folding them together
    leaves what actually happened, which is nothing.

    Only bounces reach here. A clearing posts Bank against Cheques in Hand and
    carries no party tag at all, so it never appears in a party's ledger.
    """
    event_ids = {
        key[1] for by_key in raw.values() for key in by_key if key[0] == ChequeEvent.__name__
    }
    if not event_ids:
        return

    owner = dict(ChequeEvent.objects.filter(pk__in=event_ids).values_list("pk", "payment_id"))
    for by_key in raw.values():
        for key in [k for k in list(by_key) if k[0] == ChequeEvent.__name__]:
            payment_key = (Payment.__name__, owner.get(key[1]))
            target = by_key.get(payment_key)
            if target is None:
                continue  # the receipt is outside this as_of; leave it standing
            by_key[payment_key] = replace(
                target, net_paisa=target.net_paisa + by_key.pop(key).net_paisa
            )


def _allocations_for(payment_ids, voucher_keys, *, as_of: date) -> dict:
    """Live allocations, indexed both ways, scoped to what is on the screen.

    Returned as ``(by_target, by_payment)``. An allocation is counted only when
    **both** ends are in the ledger picture for this ``as_of`` — a receipt
    applied to an invoice that had not been posted yet on that date was, on that
    date, money sitting on account. Dropping it from both indexes is what keeps
    the bills and the credits summing back to ``party_balance``.
    """
    by_target: dict[tuple[str, int], int] = {}
    by_payment: dict[int, int] = {}
    if not payment_ids:
        return by_target, by_payment

    rows = (
        PaymentAllocation.objects.live()
        .filter(payment_id__in=payment_ids, payment__posting_date__lte=as_of)
        .values_list("payment_id", "invoice_type", "invoice_id", "amount_paisa")
    )
    for payment_id, invoice_type, invoice_id, amount in rows:
        key = (invoice_type, invoice_id)
        if key not in voucher_keys:
            continue
        by_target[key] = by_target.get(key, 0) + amount
        by_payment[payment_id] = by_payment.get(payment_id, 0) + amount
    return by_target, by_payment


def _split(rows, by_target, by_payment, as_of: date, due_dates):
    """Turn one party's voucher rows into open items and credits.

    A voucher is consumed from both directions and the same arithmetic applies
    on both sides of the split: money allocated **to** it (it is somebody's
    bill) plus, when it is itself a payment, money allocated **from** it. Doing
    it identically in both branches is what makes the two halves cancel, and is
    why a refund — a payment that sits on the bill side and a credit note that
    sits on the credit side — still ties back to ``party_balance``.

    Anything fully settled is dropped. A paid invoice is not an open item, and
    since it contributes zero to both halves, dropping it changes no total.
    """
    open_items: list[OpenItem] = []
    credits: list[Credit] = []

    for row in rows:
        key = (row.voucher_type, row.voucher_id)
        consumed = by_target.get(key, 0)
        if row.voucher_type == Payment.__name__:
            consumed += by_payment.get(row.voucher_id, 0)
        if abs(row.net_paisa) == consumed:
            continue

        if row.net_paisa > 0:
            open_items.append(
                OpenItem(
                    voucher_type=row.voucher_type,
                    voucher_id=row.voucher_id,
                    voucher_code=row.voucher_code,
                    posting_date=row.posting_date,
                    due_date=due_dates.get(key, row.posting_date),
                    original_paisa=row.net_paisa,
                    allocated_paisa=consumed,
                    as_of=as_of,
                )
            )
        else:
            credits.append(
                Credit(
                    voucher_type=row.voucher_type,
                    voucher_id=row.voucher_id,
                    voucher_code=row.voucher_code,
                    posting_date=row.posting_date,
                    original_paisa=-row.net_paisa,
                    applied_paisa=consumed,
                )
            )

    open_items.sort(key=lambda item: (item.due_date, item.voucher_id))
    credits.sort(key=lambda credit: (credit.posting_date, credit.voucher_id))
    return open_items, credits


def _due_dates(voucher_keys) -> dict[tuple[str, int], date]:
    """Due dates for the documents on screen, one query per document type.

    Only the types that actually carry one are queried; everything else falls
    back to its ledger posting date in :func:`_split`.
    """
    wanted: dict[str, list[int]] = {}
    for voucher_type, voucher_id in voucher_keys:
        spec = ALLOCATABLE_DOCUMENTS.get(voucher_type)
        if spec is not None and spec.due_date_field:
            wanted.setdefault(voucher_type, []).append(voucher_id)

    due: dict[tuple[str, int], date] = {}
    for voucher_type, ids in wanted.items():
        spec = ALLOCATABLE_DOCUMENTS[voucher_type]
        rows = allocatable_model(voucher_type).objects.filter(pk__in=ids)
        for pk, value in rows.values_list("pk", spec.due_date_field):
            if value is not None:
                due[(voucher_type, pk)] = value
    return due


def open_items(party_type: str, party_id: int, *, as_of: date | None = None):
    """One party's unsettled bills and unapplied credits, aged.

    Returns ``(open_items, credits)``. The single-party entry point; the
    workspace uses :func:`recovery_rows`, which asks the same questions for a
    whole screenful in a fixed number of queries.
    """
    as_of = _as_of(as_of)
    rows = _voucher_rows(party_type, party_ids=[party_id], as_of=as_of).get(party_id, [])
    keys = {(row.voucher_type, row.voucher_id) for row in rows}
    payment_ids = [row.voucher_id for row in rows if row.voucher_type == Payment.__name__]
    by_target, by_payment = _allocations_for(payment_ids, keys, as_of=as_of)
    return _split(rows, by_target, by_payment, as_of, _due_dates(keys))


# ===========================================================================
# The workspace row
# ===========================================================================
@dataclass(frozen=True, slots=True)
class ClientRecovery:
    """One line of the recovery sheet: a shop, what it owes, and how old.

    ``outstanding_paisa`` is :func:`~apps.accounting.services.party_balance` —
    the client's ledger balance, aggregated, nothing cached. The open items and
    the on-account figure are the same number broken apart, and
    :meth:`ties_out` asserts they still add up.
    """

    client: Client
    as_of: date
    outstanding_paisa: int
    open_items: tuple[OpenItem, ...]
    credits: tuple[Credit, ...]
    last_payment_date: date | None
    last_payment_paisa: int
    last_payment_code: str
    bounced_cheque_count: int
    bounced_cheque_paisa: int

    # -- ageing ------------------------------------------------------------
    @property
    def buckets(self) -> dict[str, int]:
        """Outstanding money per ageing bucket. Every bucket present, even at 0.

        Present-even-at-zero so the workspace renders a fixed grid of columns
        rather than a ragged one that moves as you filter.
        """
        totals = dict.fromkeys(AGEING_BUCKETS, 0)
        for item in self.open_items:
            totals[item.bucket] += item.outstanding_paisa
        return totals

    @property
    def bucket_row(self) -> list[tuple[str, str, int, bool]]:
        """``(value, label, paisa, is_overdue)`` per bucket, in report order."""
        totals = self.buckets
        return [
            (bucket, AgeingBucket(bucket).label, totals[bucket], bucket in OVERDUE_BUCKETS)
            for bucket in AGEING_BUCKETS
        ]

    @property
    def overdue_paisa(self) -> int:
        """Everything past its due date. What the alarm colour is for."""
        return sum(item.outstanding_paisa for item in self.open_items if item.is_overdue)

    @property
    def current_paisa(self) -> int:
        return self.buckets[AgeingBucket.CURRENT]

    @property
    def worst_bucket(self) -> str:
        """The oldest bucket this client has money in. Sorts the sheet."""
        totals = self.buckets
        for bucket in reversed(AGEING_BUCKETS):
            if totals[bucket]:
                return bucket
        return AgeingBucket.CURRENT

    # -- the row's other columns -------------------------------------------
    @property
    def oldest_invoice_date(self) -> date | None:
        """The posting date of the oldest bill still open. ``None`` if none are."""
        if not self.open_items:
            return None
        return min(item.posting_date for item in self.open_items)

    @property
    def oldest_days(self) -> int:
        oldest = self.oldest_invoice_date
        return (self.as_of - oldest).days if oldest else 0

    @property
    def open_paisa(self) -> int:
        return sum(item.outstanding_paisa for item in self.open_items)

    @property
    def on_account_paisa(self) -> int:
        """Money and credit notes on the account that settle nothing yet."""
        return sum(credit.unapplied_paisa for credit in self.credits)

    @property
    def unapplied_credits(self) -> list[Credit]:
        return [credit for credit in self.credits if credit.unapplied_paisa]

    @property
    def has_on_account(self) -> bool:
        return self.on_account_paisa > 0

    @property
    def is_flagged(self) -> bool:
        """Whether this shop has ever handed over a cheque that bounced.

        **Derived, not a column on the client** (CLAUDE.md §6). The bounce is
        recorded as a :class:`~apps.payments.models.ChequeEvent`, so this cannot
        drift away from the events the way a flag somebody has to remember to
        set — or unset — would. It is what puts the shop in the alarm colour
        before anybody agrees to take another cheque from it.
        """
        return self.bounced_cheque_count > 0

    @property
    def phone(self) -> str:
        return self.client.phone

    def ties_out(self) -> bool:
        """Whether the breakdown still adds up to the ledger balance.

        ``open - on account == party_balance``. Asserted in the tests rather
        than at render time, because a screen that quietly corrected itself
        would hide exactly the bug worth finding — and this is the property that
        makes the whole workspace trustworthy: if the ageing ladder stops adding
        up to the shop's ledger balance, one of the two is lying.
        """
        return self.open_paisa - self.on_account_paisa == self.outstanding_paisa


def recovery_rows(
    *,
    as_of: date | None = None,
    route=None,
    routes=None,
    seller=None,
    query: str = "",
    bucket: str = "",
    include_settled: bool = False,
) -> list[ClientRecovery]:
    """The recovery sheet: one row per client with something on their account.

    Filters are the four the accountant actually uses — route, seller, a text
    search over code/name/phone, and an ageing bucket. The bucket filter is
    applied last and in Python, because which bucket a client is in is derived
    from the ledger and the due dates rather than stored anywhere.

    ``seller`` filters on the **client's** usual booker rather than on who
    collected a payment: this sheet is a list of shops to chase, and who
    happened to take the last receipt does not decide whose round it is on.

    ``routes`` is the row-level scope, not a filter the operator chose: a list
    of route ids narrows the sheet to those beats before the ledger is asked
    anything, so a booker's screen costs the same as anybody else's rather than
    aggregating the whole customer book and throwing most of it away. An empty
    list means **no shops**, which is what a scoped user with no routes must
    see. See :mod:`apps.accounts.scoping`.

    Query count is fixed and does not grow with the number of clients: one for
    the ledger, one for the allocations, one per due-date-bearing document type,
    and one for the clients (which carries the last-payment and bounced-cheque
    subqueries).
    """
    as_of = _as_of(as_of)

    clients = Client.objects.select_related("route", "seller")
    narrowed = False
    if route is not None:
        clients = clients.filter(route=route)
        narrowed = True
    if routes is not None:
        clients = clients.filter(route_id__in=routes)
        narrowed = True
    if seller is not None:
        clients = clients.filter(seller=seller)
        narrowed = True
    if query:
        clients = clients.filter(
            Q(code__icontains=query) | Q(name__icontains=query) | Q(phone__icontains=query)
        )
        narrowed = True

    # Only ask the ledger about the clients that can appear. Without a filter,
    # asking about all of them is one grouped scan rather than a giant IN list.
    party_ids = list(clients.values_list("pk", flat=True)) if narrowed else None
    by_party = _voucher_rows(PartyType.CLIENT, party_ids=party_ids, as_of=as_of)
    if not by_party:
        return []

    keys = {(row.voucher_type, row.voucher_id) for rows in by_party.values() for row in rows}
    payment_ids = [
        row.voucher_id
        for rows in by_party.values()
        for row in rows
        if row.voucher_type == Payment.__name__
    ]
    by_target, by_payment = _allocations_for(payment_ids, keys, as_of=as_of)
    due_dates = _due_dates(keys)

    rows: list[ClientRecovery] = []
    for client in _annotated_clients(clients.filter(pk__in=list(by_party)), as_of=as_of):
        vouchers = by_party[client.pk]
        items, credits = _split(vouchers, by_target, by_payment, as_of, due_dates)
        # Summed from the **raw** vouchers rather than from the split, because
        # _split drops what is fully settled: an invoice part-paid by a receipt
        # keeps the invoice and loses the receipt, and adding those two up would
        # report the whole invoice as outstanding. This is party_balance() as at
        # as_of, arrived at from rows already in hand.
        balance = sum(voucher.net_paisa for voucher in vouchers)
        row = ClientRecovery(
            client=client,
            as_of=as_of,
            outstanding_paisa=balance,
            open_items=tuple(items),
            credits=tuple(credits),
            last_payment_date=getattr(client, "_last_payment_date", None),
            last_payment_paisa=getattr(client, "_last_payment_paisa", None) or 0,
            last_payment_code=getattr(client, "_last_payment_code", None) or "",
            bounced_cheque_count=getattr(client, "_bounced_count", 0) or 0,
            bounced_cheque_paisa=getattr(client, "_bounced_paisa", 0) or 0,
        )
        if not include_settled and not row.open_paisa and not row.on_account_paisa:
            continue
        if bucket and not row.buckets.get(bucket):
            continue
        rows.append(row)

    # Oldest money first, then largest: the order somebody works down the sheet.
    order = {value: index for index, value in enumerate(AGEING_BUCKETS)}
    rows.sort(key=lambda row: (-order[row.worst_bucket], -row.open_paisa))
    return rows


def client_recovery(client, *, as_of: date | None = None) -> ClientRecovery:
    """One client's row, for the expanded view and for a statement.

    Reads ``outstanding_paisa`` straight from
    :func:`~apps.accounting.services.party_balance` rather than adding up the
    items, so an expanded row is checkable against the ledger by eye.
    """
    as_of = _as_of(as_of)
    items, credits = open_items(PartyType.CLIENT, client.pk, as_of=as_of)
    annotated = _annotated_clients(
        Client.objects.filter(pk=client.pk).select_related("route", "seller"), as_of=as_of
    )
    client = annotated[0] if annotated else client

    return ClientRecovery(
        client=client,
        as_of=as_of,
        outstanding_paisa=party_balance(PartyType.CLIENT, client.pk, as_of).paisa,
        open_items=tuple(items),
        credits=tuple(credits),
        last_payment_date=getattr(client, "_last_payment_date", None),
        last_payment_paisa=getattr(client, "_last_payment_paisa", None) or 0,
        last_payment_code=getattr(client, "_last_payment_code", None) or "",
        bounced_cheque_count=getattr(client, "_bounced_count", 0) or 0,
        bounced_cheque_paisa=getattr(client, "_bounced_paisa", 0) or 0,
    )


def _annotated_clients(clients, *, as_of: date):
    """Attach last payment and bounced-cheque history in the same query.

    Correlated subqueries rather than a second pass: a sheet of three hundred
    shops must not be three hundred round trips to find out when each of them
    last paid.
    """
    latest = (
        Payment.objects.live()
        .filter(
            party_type=PartyType.CLIENT,
            party_id=OuterRef("pk"),
            direction=PaymentDirection.RECEIVE,
            posting_date__lte=as_of,
        )
        .order_by("-posting_date", "-id")
    )
    bounced = (
        Payment.objects.filter(
            party_type=PartyType.CLIENT,
            party_id=OuterRef("pk"),
            status=DocumentStatus.POSTED,
            mode=PaymentMode.CHEQUE,
            cheque_events__status=DocumentStatus.POSTED,
            cheque_events__kind=ChequeEventKind.BOUNCED,
            posting_date__lte=as_of,
        )
        .order_by()
        .values("party_id")
    )
    return list(
        clients.annotate(
            _last_payment_date=Subquery(latest.values("posting_date")[:1]),
            _last_payment_paisa=Subquery(latest.values("amount_paisa")[:1]),
            _last_payment_code=Subquery(latest.values("code")[:1]),
            _bounced_count=Coalesce(
                Subquery(bounced.annotate(n=Count("pk")).values("n")[:1]), Value(0)
            ),
            _bounced_paisa=Coalesce(
                Subquery(bounced.annotate(t=Sum("amount_paisa")).values("t")[:1]), Value(0)
            ),
        )
    )


def ageing_summary(rows) -> list[tuple[str, str, int, bool]]:
    """The totals strip across the top of the workspace, in report order."""
    totals = dict.fromkeys(AGEING_BUCKETS, 0)
    for row in rows:
        for bucket, paisa in row.buckets.items():
            totals[bucket] += paisa
    return [
        (bucket, AgeingBucket(bucket).label, totals[bucket], bucket in OVERDUE_BUCKETS)
        for bucket in AGEING_BUCKETS
    ]


# ===========================================================================
# Today's recovery, by route
# ===========================================================================
@dataclass(frozen=True, slots=True)
class RouteRecovery:
    """One route's day: what came in against what is still out there.

    The two figures are grouped differently on purpose. **Collected** is grouped
    by the route recorded on the payment — where the money was actually
    collected, which on a covered beat is not the shop's usual route.
    **Outstanding** is grouped by the shop's own route, because that is whose
    book the debt sits in. Forcing them onto one grouping would make one of the
    two wrong on exactly the days it matters.
    """

    route: object | None
    collected_paisa: int
    payment_count: int
    outstanding_paisa: int
    client_count: int

    @property
    def route_code(self) -> str:
        return self.route.code if self.route else "—"

    @property
    def route_name(self) -> str:
        return self.route.name if self.route else "No route"

    @property
    def recovery_rate_bp(self) -> int:
        """Collected as basis points of collected + outstanding. Display only.

        Integer arithmetic, like every other percentage in this system — see
        :attr:`apps.masters.models.Item.tax_rate_display`. It is a progress bar,
        not a figure anything is posted from.
        """
        total = self.collected_paisa + self.outstanding_paisa
        return (self.collected_paisa * 10_000) // total if total else 0

    @property
    def recovery_rate_display(self) -> str:
        whole, hundredths = divmod(self.recovery_rate_bp, 100)
        return f"{whole}.{hundredths:02d}%"


def todays_recovery(*, on: date | None = None, route=None, routes=None) -> list[RouteRecovery]:
    """What each route collected on a day, against what it still has out.

    "Collected" counts live receipts only — a cheque that has bounced was never
    recovery, however good it looked on the day, and
    :meth:`~apps.payments.models.PaymentQuerySet.live` is what keeps it off this
    panel. A cheque that has simply not cleared yet **does** count: it was
    collected, it is in the drawer, and the accountant needs to see it.
    """
    on = _as_of(on)

    payments = Payment.objects.live().filter(
        party_type=PartyType.CLIENT,
        direction=PaymentDirection.RECEIVE,
        posting_date=on,
    )
    if route is not None:
        payments = payments.filter(route=route)
    if routes is not None:
        payments = payments.filter(route_id__in=routes)

    collected: dict[int | None, tuple[int, int]] = {}
    for row in payments.values("route_id").annotate(
        total=Coalesce(Sum("amount_paisa"), Value(0)), n=Count("pk")
    ):
        collected[row["route_id"]] = (row["total"], row["n"])

    # What is still out there is the shop's **balance**, not the gross of its
    # open bills: a shop that handed over Rs 2,500 this morning that nobody has
    # applied to an invoice yet still owes Rs 2,500 less. Clamped at zero so a
    # shop in credit does not quietly cancel out a neighbour's debt.
    outstanding: dict[int | None, tuple[int, int]] = {}
    for row in recovery_rows(as_of=on, route=route, routes=routes):
        route_id = row.client.route_id
        total, count = outstanding.get(route_id, (0, 0))
        outstanding[route_id] = (total + max(row.outstanding_paisa, 0), count + 1)

    route_ids = {rid for rid in (*collected, *outstanding) if rid is not None}
    routes = {r.pk: r for r in Route.objects.filter(pk__in=route_ids)}

    # Named routes in code order, then the unrouted line last — a walk-in that
    # paid at the counter belongs on this panel, not silently off it.
    ordered = [*sorted(route_ids, key=lambda pk: routes[pk].code), None]
    return [
        RouteRecovery(
            route=routes.get(route_id) if route_id is not None else None,
            collected_paisa=collected.get(route_id, (0, 0))[0],
            payment_count=collected.get(route_id, (0, 0))[1],
            outstanding_paisa=outstanding.get(route_id, (0, 0))[0],
            client_count=outstanding.get(route_id, (0, 0))[1],
        )
        for route_id in ordered
        if route_id in collected or route_id in outstanding
    ]


def day_totals(lines) -> tuple[int, int, int]:
    """``(collected, outstanding, payments)`` across every route on the panel."""
    return (
        sum(line.collected_paisa for line in lines),
        sum(line.outstanding_paisa for line in lines),
        sum(line.payment_count for line in lines),
    )


def pending_cheques(*, as_of: date | None = None, route=None, include_cancelled: bool = False):
    """Cheques in the drawer: posted, not yet cleared, oldest cheque date first.

    The cheque register. Its total is what account 1160 Cheques in Hand should
    be showing, which is a reconciliation somebody can do by eye — and the
    reason a **cancelled** receipt is not on it by default: its entries have
    already been reversed out of 1160, so counting it would break the one thing
    this list is for (CLAUDE.md §5's "excluded from the figures, never deleted").

    ``include_cancelled=True`` is the audit view, asked for explicitly. The
    cancelled rows come back and the caller is expected to say on screen that
    the total no longer matches the account.
    """
    as_of = _as_of(as_of)
    settled = ChequeEvent.objects.filter(payment_id=OuterRef("pk"), status=DocumentStatus.POSTED)
    statuses = (
        [DocumentStatus.POSTED, DocumentStatus.CANCELLED]
        if include_cancelled
        else [DocumentStatus.POSTED]
    )
    cheques = (
        Payment.objects.filter(
            status__in=statuses,
            mode=PaymentMode.CHEQUE,
            posting_date__lte=as_of,
        )
        .exclude(pk__in=Subquery(settled.values("payment_id")))
        .select_related("route", "collected_by")
        .order_by("cheque_date", "id")
    )
    if route is not None:
        cheques = cheques.filter(route=route)
    return cheques


def party_open_total(party_type: str, party_id: int, *, as_of: date | None = None) -> int:
    """One party's outstanding, straight from the ledger. The headline figure."""
    return party_balance(party_type, party_id, _as_of(as_of)).paisa


__all__ = [
    "ClientRecovery",
    "Credit",
    "OpenItem",
    "RouteRecovery",
    "ageing_summary",
    "client_recovery",
    "day_totals",
    "document_allocated_paisa",
    "document_balance_paisa",
    "document_due_date",
    "document_open_paisa",
    "open_items",
    "party_open_total",
    "pending_cheques",
    "recovery_rows",
    "todays_recovery",
]
