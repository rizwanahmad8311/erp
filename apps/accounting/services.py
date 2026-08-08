"""
The two ledgers' eight operations.

Everything that will ever be reported — a customer's outstanding balance, a
trial balance, a P&L, a recovery sheet, a stock position, a valuation — is one
of these four reads over rows written by one of these four writes. There is no
ninth operation and there is no cached total anywhere in the system
(CLAUDE.md §6).

    post_entries(voucher, lines, posting_date)   write a balanced set of rows
    reverse_entries(voucher)                     write their mirror image
    account_balance(account, as_of=None)         aggregate one account's subtree
    party_balance(party_type, party_id, as_of)   aggregate one party

    post_stock(voucher, lines, posting_date)     value and write stock movement
    reverse_stock(voucher)                       write its mirror image
    stock_balance(item, warehouse, as_of)        aggregate quantity and value
    valuation_rate(item, warehouse, as_of)       what one base unit is worth

Every write is append-only and atomic. None of them ever updates or deletes a
row; the two reversals in particular do not touch the entries they reverse,
they write new ones beside them.

There is one thing here that is neither a read of a balance nor a write:

    preview_reversal(voucher)                    the rows a cancellation would
                                                 write, without writing them

It is not a ninth operation — it is the two reversals with the insert taken
out, sharing their "which rows are still live" query so a cancel screen cannot
show one set of entries and post another.

The stock half deliberately mirrors the ledger half — same line-dict shape, same
double-post refusal, same reversal rules — with one structural difference, in
:func:`post_stock`: a ledger posting can be validated before the transaction
opens because balancing is pure arithmetic on the caller's own numbers, whereas
a stock posting has to read the position it is valuing against, and that read
belongs under the same write lock as the insert.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import NamedTuple

from django.conf import settings
from django.db import transaction
from django.db.models import Sum

from apps.accounts.permissions import OVERRIDE_NEGATIVE_STOCK
from apps.core.money import Money, fmt
from apps.masters.models import Item

from .enums import party_sign
from .exceptions import (
    AlreadyPosted,
    AlreadyReversed,
    InsufficientStock,
    InvalidPosting,
    UnbalancedEntry,
)
from .models import Account, LedgerEntry, StockEntry, Warehouse
from .refs import PartyRef, VoucherRef
from .valuation import Position

#: The keys a line dict may contain. Anything else is a typo, and a typo like
#: ``debit`` for ``debit_paisa`` would otherwise post a silent zero.
LINE_KEYS = frozenset({"account", "debit_paisa", "credit_paisa", "party", "remarks"})

#: The same, for a stock line. An inward line carries exactly one of
#: ``rate_paisa`` or ``value_paisa``; an outward line carries neither — see
#: :func:`_prepare_stock_line`.
STOCK_LINE_KEYS = frozenset({"item", "warehouse", "qty_base", "rate_paisa", "value_paisa"})


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------
def post_entries(voucher, lines, posting_date, *, user=None) -> list[LedgerEntry]:
    """Write one voucher's ledger rows. All of them, or none of them.

    ``lines`` is a list of dicts::

        post_entries(
            invoice,
            [
                {"account": receivable, "debit_paisa": 118000,
                 "party": PartyRef(PartyType.CLIENT, client.pk),
                 "remarks": "Invoice total"},
                {"account": sales, "credit_paisa": 100000},
                {"account": tax_payable, "credit_paisa": 18000},
            ],
            posting_date=invoice.invoice_date,
            user=user,
        )

    Every line carries exactly one non-zero ``debit_paisa`` **or**
    ``credit_paisa``, as a plain ``int`` of paisa. ``party`` and ``remarks`` are
    optional.

    Rules, all of them enforced rather than assumed:

    * The debits and the credits must sum to **exactly** the same number of
      paisa. Not within a paisa — exactly. :class:`UnbalancedEntry` names the
      difference, because that number tells you immediately whether you are
      looking at a rounding bug or a missing line.
    * No entry may be aimed at a group account, or at an inactive one.
    * A voucher that already has ledger rows is refused
      (:class:`AlreadyPosted`). Posting the same invoice twice is the most
      expensive accident available here and it is not detectable downstream.

    The request is validated *before* the transaction opens. SQLite runs in
    ``transaction_mode: IMMEDIATE``, so ``atomic()`` takes the database-wide
    write lock at ``BEGIN`` (CLAUDE.md §4) — there is no reason to hold it while
    finding out the caller's arithmetic was wrong.

    Returns the created rows, oldest first.
    """
    ref = VoucherRef.of(voucher)
    posting_date = _as_ledger_date(posting_date, label="posting_date")
    prepared = _prepare_lines(lines)
    _assert_balanced(prepared, ref)

    with transaction.atomic():
        if LedgerEntry.objects.filter(voucher_type=ref.type, voucher_id=ref.id).exists():
            raise AlreadyPosted(
                f"{ref} already has ledger entries. A document is posted once; to correct "
                f"it, cancel it (which reverses those entries) and post an amendment."
            )

        return LedgerEntry.objects.bulk_create(
            [
                LedgerEntry(
                    posting_date=posting_date,
                    account=line["account"],
                    debit_paisa=line["debit_paisa"],
                    credit_paisa=line["credit_paisa"],
                    party_type=line["party"].type if line["party"] else None,
                    party_id=line["party"].id if line["party"] else None,
                    voucher_type=ref.type,
                    voucher_id=ref.id,
                    voucher_code=ref.code,
                    is_reversal=False,
                    reverses=None,
                    remarks=line["remarks"],
                    created_by=user,
                )
                for line in prepared
            ]
        )


def reverse_entries(
    voucher,
    *,
    posting_date: date | None = None,
    user=None,
    remarks: str = "",
) -> list[LedgerEntry]:
    """Write the mirror image of a voucher's ledger rows.

    This is what cancelling a document does. For each live row it writes a new
    row against the same account, on the same date, for the same amount, on the
    **opposite side**: a debit of 500 is reversed by a credit of 500. Never by a
    debit of -500 — the ledger has one way to express a direction and negative
    amounts are refused by a CHECK constraint.

    The originals are not read-modify-written, not flagged, not touched in any
    way. Nothing in this function issues an UPDATE. After it runs, the account
    holds both rows and they sum to zero.

    Only rows that are *live* are reversed: rows that are not themselves
    reversals, and that nothing already reverses. So:

    * calling this twice raises :class:`AlreadyReversed` the second time —
      reversing a reversal would restore the original amounts and quietly
      un-cancel the document;
    * calling it on a voucher that was never posted raises the same, because
      there is nothing there to mirror.

    ``posting_date`` defaults to each original row's own date, which is what
    makes ``account_balance(as_of=...)`` still net to zero when you look back at
    a date before the cancellation. Pass one explicitly only when the original
    period must not be reopened; the reversal then lands on the later date and
    the earlier balance correctly still shows the original.

    Note what is deliberately *not* checked: whether the accounts involved are
    still active. An account deactivated between posting an invoice and
    cancelling it must not leave that invoice stuck in POSTED forever.
    """
    ref = VoucherRef.of(voucher)
    if posting_date is not None:
        posting_date = _as_ledger_date(posting_date, label="posting_date")

    with transaction.atomic():
        originals = live_ledger_entries(ref)

        if not originals:
            posted_anything = LedgerEntry.objects.filter(
                voucher_type=ref.type, voucher_id=ref.id
            ).exists()
            if posted_anything:
                raise AlreadyReversed(
                    f"{ref} has already been reversed; every entry it wrote is cancelled. "
                    f"Reversing again would put the original amounts back."
                )
            raise AlreadyReversed(f"{ref} has no ledger entries, so there is nothing to reverse.")

        return LedgerEntry.objects.bulk_create(
            [
                LedgerEntry(
                    posting_date=posting_date or original.posting_date,
                    account_id=original.account_id,
                    # The whole reversal, in two lines.
                    debit_paisa=original.credit_paisa,
                    credit_paisa=original.debit_paisa,
                    party_type=original.party_type,
                    party_id=original.party_id,
                    voucher_type=original.voucher_type,
                    voucher_id=original.voucher_id,
                    voucher_code=original.voucher_code,
                    is_reversal=True,
                    reverses=original,
                    remarks=remarks or f"Reversal of {original.voucher_code}",
                    created_by=user,
                )
                for original in originals
            ]
        )


# ---------------------------------------------------------------------------
# What a reversal would write
# ---------------------------------------------------------------------------
def live_ledger_entries(ref: VoucherRef) -> list[LedgerEntry]:
    """A voucher's ledger rows that nothing has reversed yet, oldest first.

    The definition of "what a cancellation would touch", in one place, because
    :func:`reverse_entries` and :func:`preview_reversal` disagreeing about it
    would mean a screen showing entries that are not the ones that get written.
    """
    return list(
        LedgerEntry.objects.filter(
            voucher_type=ref.type,
            voucher_id=ref.id,
            is_reversal=False,
            reversed_by__isnull=True,
        )
        .select_related("account")
        .order_by("pk")
    )


def live_stock_entries(ref: VoucherRef) -> list[StockEntry]:
    """The same, for the stock ledger."""
    return list(
        StockEntry.objects.filter(
            voucher_type=ref.type,
            voucher_id=ref.id,
            is_reversal=False,
            reversed_by__isnull=True,
        )
        .select_related("item", "warehouse")
        .order_by("pk")
    )


class ReversalLine(NamedTuple):
    """One ledger row a cancellation would write. Nothing is saved."""

    account: Account
    debit_paisa: int
    credit_paisa: int
    party_type: str | None
    party_id: int | None
    remarks: str
    posting_date: date
    #: The row being mirrored, so a screen can show the pair side by side.
    reverses: LedgerEntry


class ReversalStockLine(NamedTuple):
    """One stock row a cancellation would write. Nothing is saved."""

    item: object
    warehouse: object
    qty_base: int
    rate_paisa: int
    value_paisa: int
    posting_date: date
    reverses: StockEntry


class ReversalPreview(NamedTuple):
    """Exactly what cancelling a voucher would put into the two ledgers.

    Read-only, and computed by mirroring the same rows :func:`reverse_entries`
    and :func:`reverse_stock` would mirror, with the same swap: a debit becomes
    a credit of the same paisa, a stock quantity and its value negate and the
    rate rides across unchanged. The cancel screen renders this **before**
    anybody confirms, so what is agreed to is what lands.

    It is a read of the ledger as it stands. If somebody else cancels the same
    document in between, the cancellation itself refuses — the preview is not a
    reservation and does not pretend to be one.
    """

    ledger: list[ReversalLine]
    stock: list[ReversalStockLine]

    @property
    def debit_paisa(self) -> int:
        return sum(line.debit_paisa for line in self.ledger)

    @property
    def credit_paisa(self) -> int:
        return sum(line.credit_paisa for line in self.ledger)

    @property
    def balances(self) -> bool:
        """A mirror of a balanced posting is balanced. Shown, and asserted."""
        return self.debit_paisa == self.credit_paisa

    @property
    def is_empty(self) -> bool:
        return not self.ledger and not self.stock


def preview_reversal(voucher) -> ReversalPreview:
    """The rows cancelling this voucher would write. Writes nothing.

    Empty when the voucher was never posted or has already been reversed —
    which is precisely when :func:`reverse_entries` would raise
    :class:`AlreadyReversed`, so a screen can offer the button or explain why
    not without catching an exception to find out.
    """
    ref = VoucherRef.of(voucher)

    ledger = [
        ReversalLine(
            account=original.account,
            # The whole reversal, in two lines — the same swap reverse_entries
            # makes, and the reason both read live_ledger_entries().
            debit_paisa=original.credit_paisa,
            credit_paisa=original.debit_paisa,
            party_type=original.party_type,
            party_id=original.party_id,
            remarks=f"Reversal of {original.voucher_code}",
            posting_date=original.posting_date,
            reverses=original,
        )
        for original in live_ledger_entries(ref)
    ]

    stock = [
        ReversalStockLine(
            item=original.item,
            warehouse=original.warehouse,
            qty_base=-original.qty_base,
            rate_paisa=original.rate_paisa,
            value_paisa=-original.value_paisa,
            posting_date=original.posting_date,
            reverses=original,
        )
        for original in live_stock_entries(ref)
    ]

    return ReversalPreview(ledger=ledger, stock=stock)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
def account_balance(account: Account, as_of: date | None = None) -> Money:
    """What an account holds, aggregated from the ledger. Nothing is cached.

    Returns :class:`~apps.core.money.Money` in the account's **natural sign**
    (see :func:`apps.accounting.enums.account_sign`): positive means the account
    holds what its type says it should hold. Cash of ``Money(50000)`` is Rs 500
    in the drawer; Accounts Payable of ``Money(50000)`` is Rs 500 owed to
    suppliers.

    A group account totals its whole subtree, and a leaf is its own subtree, so
    callers never branch on ``is_group``. Without this, asking a heading like
    "Expenses" for its balance would return a confident and completely wrong
    zero.

    ``as_of`` is inclusive and is a ``date``, not a ``datetime`` — see
    :func:`_as_ledger_date`.
    """
    entries = LedgerEntry.objects.filter(account_id__in=account.subtree_ids())
    debit, credit = _totals(entries, as_of)
    return Money(account.natural_sign * (debit - credit))


def party_balance(party_type: str, party_id: int, as_of: date | None = None) -> Money:
    """What a client owes us, or what we owe a vendor. Aggregated, never cached.

    Returns :class:`~apps.core.money.Money` in the party's natural sign (see
    :func:`apps.accounting.enums.party_sign`): positive is always the normal
    direction of business. A client at ``Money(250000)`` owes Rs 2,500; a vendor
    at ``Money(250000)`` is owed Rs 2,500.

    This sums **every** ledger row tagged with the party, across every document
    that ever touched them — invoices, returns, receipts, discounts. Which is
    why cancellation writes reversing rows rather than deleting: a cancelled
    invoice contributes its original rows *and* their mirrors, nets to zero, and
    the party balance is right without anything having to know that a
    cancellation happened. An amendment is a different document with its own
    rows, so it simply adds.
    """
    party = PartyRef(party_type, party_id)  # validates the pair
    entries = LedgerEntry.objects.filter(party_type=party.type, party_id=party.id)
    debit, credit = _totals(entries, as_of)
    return Money(party_sign(party.type) * (debit - credit))


def _totals(entries, as_of: date | None) -> tuple[int, int]:
    """``(debit, credit)`` paisa for a queryset, optionally up to a date."""
    if as_of is not None:
        entries = entries.filter(posting_date__lte=_as_ledger_date(as_of, label="as_of"))
    totals = entries.aggregate(
        debit=Sum("debit_paisa", default=0),
        credit=Sum("credit_paisa", default=0),
    )
    return totals["debit"], totals["credit"]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def _as_ledger_date(value, *, label: str) -> date:
    """Insist on a plain ``date``.

    ``datetime`` is rejected even though it is a subclass of ``date``, and that
    rejection is load-bearing. Timestamps here are timezone-aware
    (``USE_TZ = True``, ``TIME_ZONE = "Asia/Karachi"``); an 11pm Karachi
    ``datetime`` converted to UTC lands on the previous day. Which day a sale
    hit the books is not a question that may depend on which timezone the
    conversion happened to run in.
    """
    if isinstance(value, datetime):
        raise InvalidPosting(
            f"{label} must be a date, not a datetime — a timezone conversion can move it "
            f"to the wrong day. Pass timezone.localdate() or .date()."
        )
    if not isinstance(value, date):
        raise InvalidPosting(f"{label} must be a date, got {type(value).__name__}: {value!r}")
    return value


def _prepare_lines(lines) -> list[dict]:
    """Validate and normalise the caller's line dicts. Reads only, no writes."""
    if lines is None:
        raise InvalidPosting("post_entries needs a list of lines; got None.")

    prepared = [_prepare_line(index, line) for index, line in enumerate(lines)]

    if not prepared:
        raise InvalidPosting(
            "post_entries was called with no lines. A voucher that moves no money should "
            "not reach the ledger at all."
        )
    return prepared


def _prepare_line(index: int, line) -> dict:
    where = f"line {index}"

    if not isinstance(line, dict):
        raise InvalidPosting(f"{where} must be a dict, got {type(line).__name__}: {line!r}")

    unknown = set(line) - LINE_KEYS
    if unknown:
        raise InvalidPosting(
            f"{where} has unknown key(s) {sorted(unknown)}; expected some of "
            f"{sorted(LINE_KEYS)}. Amounts are named debit_paisa / credit_paisa — the "
            f"unit is part of the name (CLAUDE.md §1)."
        )

    account = line.get("account")
    if not isinstance(account, Account):
        raise InvalidPosting(
            f"{where} needs an Account instance under 'account', got "
            f"{type(account).__name__}: {account!r}"
        )
    account.assert_postable()

    debit = _as_paisa(line.get("debit_paisa", 0), f"{where} debit_paisa")
    credit = _as_paisa(line.get("credit_paisa", 0), f"{where} credit_paisa")

    if debit and credit:
        raise InvalidPosting(
            f"{where} sets both sides (debit_paisa={debit}, credit_paisa={credit}). "
            f"A ledger row is one debit or one credit; split it into two lines."
        )
    if not debit and not credit:
        raise InvalidPosting(
            f"{where} moves no money. A zero line adds nothing to the ledger and hides "
            f"the fact that something upstream computed zero."
        )

    remarks = line.get("remarks") or ""
    if not isinstance(remarks, str):
        raise InvalidPosting(f"{where} remarks must be a string, got {type(remarks).__name__}")

    return {
        "account": account,
        "debit_paisa": debit,
        "credit_paisa": credit,
        "party": PartyRef.coerce(line.get("party")),
        "remarks": remarks,
    }


def _as_paisa(value, label: str) -> int:
    """A ledger amount is a non-negative whole number of paisa. Nothing else."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidPosting(
            f"{label} must be whole paisa as an int, got {type(value).__name__}: {value!r}. "
            f"If you are holding a Money, pass its .paisa; if you are holding operator "
            f"input, run it through apps.core.money.to_paisa first."
        )
    if value < 0:
        raise InvalidPosting(
            f"{label} is {value}. Ledger amounts are never negative — a reduction is a "
            f"line on the other side, and a reversal is a mirrored row."
        )
    return value


def _assert_balanced(prepared: list[dict], ref: VoucherRef) -> None:
    """Debits == credits, to the paisa.

    Summed as :class:`~apps.core.money.Money` rather than as bare ints so the
    arithmetic here obeys the same rule as every other service (CLAUDE.md §1)
    and cannot accidentally add a rupee figure to a paisa one.
    """
    debits = sum((Money(line["debit_paisa"]) for line in prepared), Money.zero())
    credits = sum((Money(line["credit_paisa"]) for line in prepared), Money.zero())

    if debits == credits:
        return

    difference = debits - credits
    direction = "excess debit" if difference.paisa > 0 else "excess credit"
    raise UnbalancedEntry(
        f"{ref} does not balance: debits {debits.paisa} paisa vs credits "
        f"{credits.paisa} paisa — a difference of {difference.paisa} paisa "
        f"({fmt(abs(difference.paisa))} {direction}). Nothing was written."
    )


# ===========================================================================
# Stock
# ===========================================================================
class StockBalance(NamedTuple):
    """What an ``(item, warehouse)`` holds: base units, and the cost behind them.

    Unpacks as the plain pair the callers want::

        qty_base, value_paisa = stock_balance(item, warehouse)

    Both are plain ``int``, matching what the fields store. Wrap the value in
    :class:`~apps.core.money.Money` to do arithmetic with it (CLAUDE.md §1).
    """

    qty_base: int
    value_paisa: int


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------
def post_stock(voucher, lines, posting_date, *, user=None) -> list[StockEntry]:
    """Value and write one voucher's stock rows. All of them, or none of them.

    ``lines`` is a list of dicts::

        post_stock(
            receipt,
            [{"item": rice, "warehouse": godown, "qty_base": 100, "rate_paisa": 8500}],
            posting_date=receipt.receipt_date,
            user=user,
        )

        post_stock(
            invoice,
            [{"item": rice, "warehouse": van, "qty_base": -12}],   # no rate
            posting_date=invoice.invoice_date,
        )

    ``qty_base`` is signed: **positive in, negative out**.

    Incoming lines must say what the goods cost, in exactly one of two ways.
    Either ``rate_paisa``, the cost of one base unit, or ``value_paisa``, the
    cost of the whole line — never both. The rate is the ordinary case; the
    total is for a document whose money is exact and whose per-unit rate is a
    rounded derivation of it, which is every purchase invoice billed by the
    carton. See :meth:`~apps.accounting.valuation.Position.receive_at_value`.
    Either way it is an *input*: nothing else can tell you what the goods cost.

    Outgoing lines must **not** carry a rate, and that refusal is load-bearing.
    An issue is valued at the moving weighted average for that
    ``(item, warehouse)`` at that moment, computed here and stored on the row.
    The rate a sales line knows is the *selling* price, and accepting it would
    value cost of goods sold at the price it sold for and report a gross margin
    of exactly zero, forever, on every invoice.

    Rules, all enforced rather than assumed:

    * A voucher that already has stock rows is refused (:class:`AlreadyPosted`),
      for the same reason a double ledger posting is: the document looks right
      and the stock is silently doubled.
    * An issue that would take an ``(item, warehouse)`` balance below zero
      raises :class:`InsufficientStock`, naming the item and how much there
      actually is — unless ``settings.ALLOW_NEGATIVE_STOCK`` is on.
    * Several lines for the same ``(item, warehouse)`` value against each other
      in order, so a receipt and an issue of the same item in one voucher behave
      exactly as they would in two.

    Unlike :func:`post_entries`, the checks that need the database run *inside*
    the transaction. A ledger posting balances or it does not, using only the
    caller's own numbers; a stock posting has to read the position it is valuing
    against, and reading that outside the write lock would let two concurrent
    issues each see stock that only one of them can have. The cheap shape checks
    still run first, outside.

    Returns the created rows, oldest first.
    """
    ref = VoucherRef.of(voucher)
    posting_date = _as_ledger_date(posting_date, label="posting_date")
    prepared = _prepare_stock_lines(lines)

    with transaction.atomic():
        if StockEntry.objects.filter(voucher_type=ref.type, voucher_id=ref.id).exists():
            raise AlreadyPosted(
                f"{ref} already has stock entries. A document is posted once; to correct "
                f"it, cancel it (which reverses those entries) and post an amendment."
            )

        # Two ways a warehouse may go under: the installation-wide switch, and
        # a person who holds the permission. ``user=None`` is trusted code — a
        # management command, a data migration, a test harness — and the same
        # convention every other permission check in this codebase uses.
        allow_negative = getattr(settings, "ALLOW_NEGATIVE_STOCK", False) or (
            user is not None and user.has_perm(OVERRIDE_NEGATIVE_STOCK)
        )
        positions: dict[tuple[int, int], _RunningPosition] = {}
        rows = []

        for line in prepared:
            item, warehouse = line["item"], line["warehouse"]
            key = (item.pk, warehouse.pk)
            if key not in positions:
                positions[key] = _RunningPosition(item, warehouse, posting_date)
            position = positions[key]

            if line["qty_base"] > 0:
                if line["value_paisa"] is not None:
                    movement = position.receive_at_value(line["qty_base"], line["value_paisa"])
                else:
                    movement = position.receive(line["qty_base"], line["rate_paisa"])
            else:
                movement = position.issue(-line["qty_base"], allow_negative=allow_negative)

            rows.append(
                StockEntry(
                    posting_date=posting_date,
                    item=item,
                    warehouse=warehouse,
                    qty_base=movement.qty_base,
                    rate_paisa=movement.rate_paisa,
                    value_paisa=movement.value_paisa,
                    voucher_type=ref.type,
                    voucher_id=ref.id,
                    voucher_code=ref.code,
                    is_reversal=False,
                    reverses=None,
                    created_by=user,
                )
            )

        return StockEntry.objects.bulk_create(rows)


def reverse_stock(
    voucher,
    *,
    posting_date: date | None = None,
    user=None,
) -> list[StockEntry]:
    """Write the mirror image of a voucher's stock rows.

    This is what cancelling a document does. For each live row it writes a new
    row for the same item in the same warehouse, on the same date, with the
    quantity and the value **negated** and the rate **carried across unchanged**.

    Carrying the rate rather than recomputing it is the whole point. A cancelled
    issue puts back exactly the value it took out, so quantity and value return
    together and the position lands where it started. Re-valuing the reversal at
    today's average would put back a different number of paisa than was removed,
    and the difference would sit in inventory forever with nothing to explain it.

    The originals are not read-modify-written, not flagged, not touched in any
    way. Nothing in this function issues an UPDATE.

    Only *live* rows are reversed — rows that are not themselves reversals and
    that nothing already reverses — so calling this twice raises
    :class:`AlreadyReversed` the second time, as does calling it on a voucher
    that was never posted.

    ``posting_date`` defaults to each original row's own date, which is what
    keeps ``stock_balance(as_of=...)`` netting to zero when you look back at a
    date before the cancellation.

    Note what is deliberately **not** checked: whether the reversal takes stock
    negative. Cancelling a goods receipt whose stock has since been sold does
    exactly that. Refusing it would trap the document in POSTED with no legal
    move left — the same reason :func:`reverse_entries` does not re-check that
    the accounts are still active. A document must always be cancellable.
    """
    ref = VoucherRef.of(voucher)
    if posting_date is not None:
        posting_date = _as_ledger_date(posting_date, label="posting_date")

    with transaction.atomic():
        originals = live_stock_entries(ref)

        if not originals:
            posted_anything = StockEntry.objects.filter(
                voucher_type=ref.type, voucher_id=ref.id
            ).exists()
            if posted_anything:
                raise AlreadyReversed(
                    f"{ref} has already been reversed; every stock entry it wrote is "
                    f"cancelled. Reversing again would put the original movement back."
                )
            raise AlreadyReversed(f"{ref} has no stock entries, so there is nothing to reverse.")

        return StockEntry.objects.bulk_create(
            [
                StockEntry(
                    posting_date=posting_date or original.posting_date,
                    item_id=original.item_id,
                    warehouse_id=original.warehouse_id,
                    # The whole reversal, in three lines: quantity and value
                    # flip, the rate is a fact about the original and does not.
                    qty_base=-original.qty_base,
                    rate_paisa=original.rate_paisa,
                    value_paisa=-original.value_paisa,
                    voucher_type=original.voucher_type,
                    voucher_id=original.voucher_id,
                    voucher_code=original.voucher_code,
                    is_reversal=True,
                    reverses=original,
                    created_by=user,
                )
                for original in originals
            ]
        )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
def stock_balance(item, warehouse=None, as_of: date | None = None) -> StockBalance:
    """What is held and what it is worth, aggregated from the stock ledger.

    Returns ``(qty_base, value_paisa)``. Nothing is cached and there is no
    ``stock_on_hand`` field anywhere to disagree with this (CLAUDE.md §6).

    ``warehouse=None`` totals every warehouse, which is the stock position
    report. The quantity is meaningful across warehouses and so is the value;
    the *rate* is not, which is why :func:`valuation_rate` insists on one
    warehouse.

    ``as_of`` is inclusive and is a ``date``, not a ``datetime`` — see
    :func:`_as_ledger_date`.
    """
    entries = StockEntry.objects.filter(item=item)
    if warehouse is not None:
        entries = entries.filter(warehouse=warehouse)
    if as_of is not None:
        entries = entries.filter(posting_date__lte=_as_ledger_date(as_of, label="as_of"))

    totals = entries.aggregate(
        qty=Sum("qty_base", default=0),
        value=Sum("value_paisa", default=0),
    )
    return StockBalance(qty_base=totals["qty"], value_paisa=totals["value"])


def valuation_rate(item, warehouse, as_of: date | None = None) -> int:
    """What one base unit of ``item`` in ``warehouse`` is worth, in paisa.

    The moving weighted average: the value held divided by the quantity held,
    rounded once through :func:`~apps.core.money.round_paisa`. Per warehouse,
    never across them — see :class:`~apps.accounting.models.Warehouse`.

    When nothing is held there is nothing to average, so the answer falls back
    to the rate on the most recent movement — the last price this item was
    known to be worth here, which is what an issue against an empty position
    has to be valued at. With no movement at all it is zero.
    """
    qty_base, value_paisa = stock_balance(item, warehouse, as_of)
    if qty_base > 0 and value_paisa > 0:
        return Position(qty_base=qty_base, value_paisa=value_paisa).rate_paisa
    return _last_rate_paisa(StockEntry.objects.filter(item=item, warehouse=warehouse), as_of)


# ---------------------------------------------------------------------------
# Valuation state
# ---------------------------------------------------------------------------
class _RunningPosition:
    """One ``(item, warehouse)`` being walked through a voucher's lines.

    Seeded from the rows that already sit **on or before** the posting date, and
    advanced line by line so that two lines touching the same item in one
    voucher value against each other in order.

    "On or before", rather than "everything written so far", is what makes
    back-dated entries value correctly. A stock card is read in posting-date
    order, so an entry dated April is valued from what April knew, however long
    after the June rows it happened to be typed in. The June rows keep the rates
    they were written with: they are history, they are append-only, and
    CLAUDE.md §3 does not have an exception for "but the average changed". What
    a back-dated entry can never do is silently re-value what is already posted.
    """

    def __init__(self, item, warehouse, posting_date: date):
        self.item = item
        self.warehouse = warehouse
        entries = StockEntry.objects.filter(item=item, warehouse=warehouse)

        self.position = _position_of(entries.filter(posting_date__lte=posting_date))
        self.fallback_rate_paisa = _last_rate_paisa(entries, posting_date)
        self.headroom = _headroom_after(entries, posting_date)

    @property
    def available(self) -> int:
        """How much may be issued without any later balance going negative.

        Normally just what is on hand. When rows already exist *after* this
        posting date, issuing here lowers every one of those later balances by
        the same amount, so the figure that matters is the worst of them — see
        :func:`_headroom_after`.
        """
        return self.position.qty_base + self.headroom

    def receive(self, qty_base: int, rate_paisa: int):
        movement = self.position.receive(qty_base, rate_paisa)
        self.position = self.position.apply(movement)
        return movement

    def receive_at_value(self, qty_base: int, value_paisa: int):
        movement = self.position.receive_at_value(qty_base, value_paisa)
        self.position = self.position.apply(movement)
        return movement

    def issue(self, qty_base: int, *, allow_negative: bool):
        if not allow_negative and qty_base > self.available:
            raise InsufficientStock(
                item=self.item,
                warehouse=self.warehouse,
                requested=qty_base,
                available=self.available,
            )
        movement = self.position.issue(qty_base, fallback_rate_paisa=self.fallback_rate_paisa)
        self.position = self.position.apply(movement)
        return movement


def _position_of(entries) -> Position:
    """Collapse a queryset of rows into the position they add up to."""
    totals = entries.aggregate(
        qty=Sum("qty_base", default=0),
        value=Sum("value_paisa", default=0),
    )
    return Position(qty_base=totals["qty"], value_paisa=totals["value"])


def _last_rate_paisa(entries, as_of: date | None) -> int:
    """The rate on the most recent row up to ``as_of``, or 0 if there is none."""
    if as_of is not None:
        entries = entries.filter(posting_date__lte=_as_ledger_date(as_of, label="as_of"))
    rate = entries.order_by("-posting_date", "-id").values_list("rate_paisa", flat=True).first()
    return rate or 0


def _headroom_after(entries, posting_date: date) -> int:
    """The worst dip in the balance *after* ``posting_date``, as a non-positive int.

    An issue back-dated into the middle of a stock card lowers every balance
    from that day on, so checking only the balance on the day itself is not
    enough: April can be comfortably in stock while May is already at zero.

    Walking the later rows costs one query that returns nothing at all in the
    ordinary case — a document posted today has no rows after it — so only
    back-dating pays for this.
    """
    running = 0
    floor = 0
    later = (
        entries.filter(posting_date__gt=posting_date)
        .order_by("posting_date", "id")
        .values_list("qty_base", flat=True)
    )
    for qty_base in later:
        running += qty_base
        floor = min(floor, running)
    return floor


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def _prepare_stock_lines(lines) -> list[dict]:
    """Validate and normalise the caller's stock lines. Reads only, no writes."""
    if lines is None:
        raise InvalidPosting("post_stock needs a list of lines; got None.")

    prepared = [_prepare_stock_line(index, line) for index, line in enumerate(lines)]

    if not prepared:
        raise InvalidPosting(
            "post_stock was called with no lines. A voucher that moves no stock should not "
            "reach the stock ledger at all."
        )
    return prepared


def _prepare_stock_line(index: int, line) -> dict:
    where = f"line {index}"

    if not isinstance(line, dict):
        raise InvalidPosting(f"{where} must be a dict, got {type(line).__name__}: {line!r}")

    unknown = set(line) - STOCK_LINE_KEYS
    if unknown:
        raise InvalidPosting(
            f"{where} has unknown key(s) {sorted(unknown)}; expected some of "
            f"{sorted(STOCK_LINE_KEYS)}. Quantities are named qty_base and rates rate_paisa "
            f"— the unit is part of the name (CLAUDE.md §1, §2)."
        )

    item = line.get("item")
    if not isinstance(item, Item):
        raise InvalidPosting(
            f"{where} needs an Item instance under 'item', got {type(item).__name__}: {item!r}"
        )

    warehouse = line.get("warehouse")
    if not isinstance(warehouse, Warehouse):
        raise InvalidPosting(
            f"{where} needs a Warehouse instance under 'warehouse', got "
            f"{type(warehouse).__name__}: {warehouse!r}. Warehouse.get_default() if the "
            f"document does not name one."
        )

    qty_base = _as_qty(line.get("qty_base", 0), f"{where} qty_base")
    if qty_base == 0:
        raise InvalidPosting(
            f"{where} moves no stock. A zero line adds nothing to the stock card and hides "
            f"the fact that something upstream computed zero."
        )

    rate_paisa = None
    value_paisa = None

    if qty_base > 0:
        # Two ways to say what incoming stock cost, and exactly one of them per
        # line. A rate, when the document knows the price of one base unit; a
        # total, when it knows the money and the rate is a rounded derivation of
        # it — see Position.receive_at_value. Accepting both would mean a row
        # where qty x rate and value disagree with no way to tell which was
        # meant.
        has_rate = "rate_paisa" in line
        has_value = "value_paisa" in line
        if has_rate and has_value:
            raise InvalidPosting(
                f"{where} supplies both rate_paisa and value_paisa. Give the rate when the "
                f"cost of one base unit is known exactly, or the total when the money is "
                f"known exactly and the rate is derived from it — never both."
            )
        if not has_rate and not has_value:
            raise InvalidPosting(
                f"{where} receives {qty_base} base unit(s) but gives neither rate_paisa nor "
                f"value_paisa. Incoming stock is valued at what it cost, and nothing but the "
                f"document knows that."
            )
        if has_rate:
            rate_paisa = _as_rate_paisa(line["rate_paisa"], f"{where} rate_paisa")
        else:
            value_paisa = _as_value_paisa(line["value_paisa"], f"{where} value_paisa")
    else:
        if "rate_paisa" in line or "value_paisa" in line:
            raise InvalidPosting(
                f"{where} issues stock and also supplies a cost. An issue is valued at the "
                f"moving average for that item and warehouse at that moment; a selling price "
                f"must never reach the stock ledger, or cost of goods sold becomes the sale."
            )

    return {
        "item": item,
        "warehouse": warehouse,
        "qty_base": qty_base,
        "rate_paisa": rate_paisa,
        "value_paisa": value_paisa,
    }


def _as_qty(value, label: str) -> int:
    """A stock quantity is a whole number of base units. Nothing else.

    Fractions are refused rather than rounded (CLAUDE.md §2): there is no half a
    piece, and a float here means a carton conversion was done by division
    somewhere upstream instead of by the item's UOM.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidPosting(
            f"{label} must be whole base units as an int, got {type(value).__name__}: "
            f"{value!r}. A pack of 12 is a UOM conversion on the item, not a fraction."
        )
    return value


def _as_rate_paisa(value, label: str) -> int:
    """A cost rate is a non-negative whole number of paisa per base unit."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidPosting(
            f"{label} must be whole paisa as an int, got {type(value).__name__}: {value!r}. "
            f"If you are holding a Money, pass its .paisa; if you are holding operator "
            f"input, run it through apps.core.money.to_paisa first."
        )
    if value < 0:
        raise InvalidPosting(
            f"{label} is {value}. A cost rate is never negative — direction lives on "
            f"qty_base, and goods coming back are an issue, not a receipt at a minus rate."
        )
    return value


def _as_value_paisa(value, label: str) -> int:
    """The total cost of an inward line: a non-negative whole number of paisa."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidPosting(
            f"{label} must be whole paisa as an int, got {type(value).__name__}: {value!r}. "
            f"If you are holding a Money, pass its .paisa."
        )
    if value < 0:
        raise InvalidPosting(
            f"{label} is {value}. Incoming stock never carries value out — direction lives "
            f"on qty_base, and goods going back to a supplier are an issue."
        )
    return value


#: The whole public surface. Accounts, entries, warehouses, ``PartyRef`` and
#: ``PartyType`` are imported from the modules that define them — this one is
#: deliberately not a facade, so that a reader can always tell where a name
#: comes from.
__all__ = [
    "ReversalLine",
    "ReversalPreview",
    "ReversalStockLine",
    "StockBalance",
    "account_balance",
    "live_ledger_entries",
    "live_stock_entries",
    "party_balance",
    "post_entries",
    "post_stock",
    "preview_reversal",
    "reverse_entries",
    "reverse_stock",
    "stock_balance",
    "valuation_rate",
]
