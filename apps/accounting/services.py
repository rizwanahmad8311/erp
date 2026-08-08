"""
The general ledger's four operations.

Everything that will ever be reported — a customer's outstanding balance, a
trial balance, a P&L, a recovery sheet — is one of these two reads over rows
written by one of these two writes. There is no fifth operation and there is no
cached total anywhere in the system (CLAUDE.md §6).

    post_entries(voucher, lines, posting_date)   write a balanced set of rows
    reverse_entries(voucher)                     write their mirror image
    account_balance(account, as_of=None)         aggregate one account's subtree
    party_balance(party_type, party_id, as_of)   aggregate one party

Both writes are append-only and atomic. Neither ever updates or deletes a row;
``reverse_entries`` in particular does not touch the entries it reverses, it
writes new ones beside them.
"""

from __future__ import annotations

from datetime import date, datetime

from django.db import transaction
from django.db.models import Sum

from apps.core.money import Money, fmt

from .enums import party_sign
from .exceptions import (
    AlreadyPosted,
    AlreadyReversed,
    InvalidPosting,
    UnbalancedEntry,
)
from .models import Account, LedgerEntry
from .refs import PartyRef, VoucherRef

#: The keys a line dict may contain. Anything else is a typo, and a typo like
#: ``debit`` for ``debit_paisa`` would otherwise post a silent zero.
LINE_KEYS = frozenset({"account", "debit_paisa", "credit_paisa", "party", "remarks"})


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
        originals = list(
            LedgerEntry.objects.filter(
                voucher_type=ref.type,
                voucher_id=ref.id,
                is_reversal=False,
                reversed_by__isnull=True,
            ).order_by("pk")
        )

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


#: The whole public surface. Accounts, entries, ``PartyRef`` and ``PartyType``
#: are imported from the modules that define them — this one is deliberately not
#: a facade, so that a reader can always tell where a name comes from.
__all__ = ["account_balance", "party_balance", "post_entries", "reverse_entries"]
