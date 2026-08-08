"""Receipts, payments, allocation and the cheque lifecycle.

Everything that writes a ledger row for a payment is in this module, wrapped in
``transaction.atomic()`` (CLAUDE.md §4). Views and admin actions call these
functions; they never build a ledger row themselves.

Three ideas hold the app together.

**A payment posts two rows and nothing else.** No stock, no lines, no
allocations::

    RECEIVE   Dr Cash / Bank / Cheques in Hand,  Cr the party account
    PAY       Dr the party account,              Cr Cash / Bank / Cheques Issued

The party account is Accounts Receivable for a client and Accounts Payable for a
vendor — always, whichever way the money went. The direction decides which side
it lands on. That is what makes a vendor's refund and a customer's refund
ordinary postings instead of two special cases nobody tested.

**Allocation is not posting.** Applying a receipt to three invoices moves no
money: the ledger already knows the client owes Rs 40,000 less. Which bills that
Rs 40,000 clears is bookkeeping about *documents*, and it stays editable after
the payment is posted — which is what makes the recovery workspace possible at
all. The unallocated remainder is an **on-account balance**: normal, visible,
and never silently swallowed.

**A cheque is not money.** It posts to Cheques in Hand and only reaches the bank
when :func:`clear_cheque` says the bank took it, which in this business is
routinely three weeks later. :func:`bounce_cheque` writes the payment's posting
back the other way and stops it settling anything. Neither one edits the payment
— a POSTED document is immutable (CLAUDE.md §5), and both of these are things
that happened on a later day and deserve their own date in the books.
"""

from __future__ import annotations

from datetime import date
from typing import NamedTuple

from django.db import transaction

from apps.accounting import chart as coa
from apps.accounting.enums import PartyType
from apps.accounting.posting import GLLine, accounts_by_code, assert_gl_balances, drop_zero_lines
from apps.accounting.services import post_entries, reverse_entries
from apps.core.enums import DocumentStatus
from apps.core.money import Money, fmt
from apps.core.services import get_next_code

from .enums import (
    CHEQUE_EVENT_PREFIX,
    PAYMENT_PREFIX,
    RECEIPT_PREFIX,
    ChequeEventKind,
    ChequeStatus,
    PaymentDirection,
    PaymentMode,
)
from .exceptions import ChequeStateError, InvalidPayment, NotAllocatable, OverAllocated
from .models import (
    ALLOCATABLE_DOCUMENTS,
    ChequeEvent,
    Payment,
    PaymentAllocation,
    allocatable_model,
    allocatable_spec,
    party_model,
    party_type_of,
)
from .recovery import (
    document_allocated_paisa,
    document_balance_paisa,
    document_open_paisa,
    open_items,
)

#: Where money of each mode sits when it is received. The pair below is the
#: whole of "which account does this land in" and it lives in one place.
RECEIVE_ACCOUNTS: dict[str, str] = {
    PaymentMode.CASH: coa.CASH,
    PaymentMode.BANK: coa.BANK,
    PaymentMode.CHEQUE: coa.CHEQUES_IN_HAND,
}

#: And where it sits when it is paid out. Only the cheque differs: one we hold
#: is an asset, one we have written is a liability until it is presented.
PAY_ACCOUNTS: dict[str, str] = {
    PaymentMode.CASH: coa.CASH,
    PaymentMode.BANK: coa.BANK,
    PaymentMode.CHEQUE: coa.CHEQUES_ISSUED,
}

#: The account a party's balance lives on. Not a function of the direction.
PARTY_ACCOUNTS: dict[str, str] = {
    PartyType.CLIENT: coa.ACCOUNTS_RECEIVABLE,
    PartyType.VENDOR: coa.ACCOUNTS_PAYABLE,
}


def money_account_code(payment) -> str:
    """Which account the money side of this payment lands in."""
    table = RECEIVE_ACCOUNTS if payment.direction == PaymentDirection.RECEIVE else PAY_ACCOUNTS
    try:
        return table[payment.mode]
    except KeyError:
        raise InvalidPayment(
            f"Unknown payment mode {payment.mode!r}; expected one of {sorted(table)}."
        ) from None


def party_account_code(party_type: str) -> str:
    """Receivable for a client, payable for a vendor. Never the other way round."""
    try:
        return PARTY_ACCOUNTS[party_type]
    except KeyError:
        raise InvalidPayment(
            f"Unknown party type {party_type!r}; expected one of {sorted(PARTY_ACCOUNTS)}."
        ) from None


def prefix_for(direction: str) -> str:
    """``RV`` for money in, ``PV`` for money out."""
    if direction == PaymentDirection.RECEIVE:
        return RECEIPT_PREFIX
    if direction == PaymentDirection.PAY:
        return PAYMENT_PREFIX
    raise InvalidPayment(f"Unknown direction {direction!r}; expected RECEIVE or PAY.")


def fiscal_year_of(posting_date: date) -> int:
    """Which numbering year a document belongs to.

    The calendar year of the posting date, matching
    :func:`apps.sales.services.fiscal_year_of`. When this installation gets a
    real fiscal-year policy, all of them change together.
    """
    return posting_date.year


# ===========================================================================
# The general ledger side
# ===========================================================================
def build_payment_gl(payment, *, amount_paisa=None) -> list[GLLine]:
    """The two rows a payment posts.

        RECEIVE   Dr money account          Cr party account (tagged with the party)
        PAY       Dr party account          Cr money account

    Balances by construction — it is the same number twice — which is why there
    is nothing to round here and nothing that could fail to balance. It is
    asserted anyway, because a posting service that trusts its own arithmetic is
    a posting service that finds out in a year.

    ``amount_paisa`` is accepted so an entry screen can preview a payment that
    has not been saved yet.
    """
    amount = Money(payment.amount_paisa if amount_paisa is None else amount_paisa)
    money_code = money_account_code(payment)
    party_code = party_account_code(payment.party_type)
    account = accounts_by_code(money_code, party_code)

    money_label = payment.get_mode_display()
    preposition = "From" if payment.direction == PaymentDirection.RECEIVE else "To"
    party_label = f"{preposition} {payment.party_name}"

    if payment.direction == PaymentDirection.RECEIVE:
        gl = [
            GLLine(account[money_code], amount.paisa, 0, money_label),
            GLLine(account[party_code], 0, amount.paisa, party_label),
        ]
    else:
        gl = [
            GLLine(account[party_code], amount.paisa, 0, party_label),
            GLLine(account[money_code], 0, amount.paisa, money_label),
        ]
    return drop_zero_lines(gl)


def build_cheque_event_gl(event) -> list[GLLine]:
    """The two rows a cheque event posts.

        CLEARED, RECEIVE   Dr Bank                 Cr Cheques in Hand
        CLEARED, PAY       Dr Cheques Issued       Cr Bank
        BOUNCED, RECEIVE   Dr Accounts Receivable  Cr Cheques in Hand
        BOUNCED, PAY       Dr Cheques Issued       Cr Accounts Payable

    Clearing swaps the cheque for the bank. Bouncing swaps it back for the
    receivable it came from, which is the payment's own posting written the
    other way — the client owes the money again, and the drawer is emptied of a
    cheque that turned out to be a piece of paper.

    The bounce rows carry the party tag, exactly as the payment's did. Without
    it the client's balance would go up in the general ledger and not in their
    statement, and the two would disagree forever.
    """
    payment = event.payment
    amount = Money(payment.amount_paisa)
    cheque_code = money_account_code(payment)

    if event.kind == ChequeEventKind.CLEARED:
        other_code = coa.BANK
        label = f"Cheque {payment.cheque_no} cleared"
    else:
        other_code = party_account_code(payment.party_type)
        label = f"Cheque {payment.cheque_no} bounced — {payment.party_name}"

    account = accounts_by_code(cheque_code, other_code)

    if payment.direction == PaymentDirection.RECEIVE:
        # The cheque leaves Cheques in Hand either way; what it becomes differs.
        gl = [
            GLLine(account[other_code], amount.paisa, 0, label),
            GLLine(account[cheque_code], 0, amount.paisa, f"Cheque {payment.cheque_no}"),
        ]
    else:
        # Ours was a liability; it is discharged either way.
        gl = [
            GLLine(account[cheque_code], amount.paisa, 0, f"Cheque {payment.cheque_no}"),
            GLLine(account[other_code], 0, amount.paisa, label),
        ]
    return drop_zero_lines(gl)


def _party_tagged(gl_lines, payment):
    """The entry dicts ``post_entries`` wants, with the party on the party line.

    Only the receivable/payable row is tagged. Tagging the cash row too would
    double every party balance, since :func:`~apps.accounting.services.party_balance`
    sums every row carrying the tag.
    """
    party_code = party_account_code(payment.party_type)
    party = payment.party_ref
    return [gl.as_entry(party if gl.account.code == party_code else None) for gl in gl_lines]


# ===========================================================================
# Payment lifecycle
# ===========================================================================
@transaction.atomic
def create_payment(
    *,
    party,
    direction: str,
    mode: str,
    posting_date: date,
    amount_paisa: int,
    **fields,
) -> Payment:
    """A new DRAFT payment with a freshly allocated code.

    ``party`` is a :class:`~apps.masters.models.Client` or
    :class:`~apps.masters.models.Vendor` instance — the soft ``(type, id)`` pair
    is derived from it here, so no caller has to remember which string goes with
    which table.

    The code comes from :func:`apps.core.services.get_next_code` inside this
    same transaction, so a failed save does not burn a number (CLAUDE.md §5).
    Receipts are numbered ``RV-…`` and payments ``PV-…``, on separate sequences.
    """
    party_type = party_type_of(party)
    payment = Payment(
        code=get_next_code(prefix_for(direction), fiscal_year_of(posting_date)),
        party_type=party_type,
        party_id=party.pk,
        direction=direction,
        mode=mode,
        posting_date=posting_date,
        amount_paisa=amount_paisa,
        **fields,
    )
    payment.apply_party_defaults(party)
    payment.save()
    return payment


@transaction.atomic
def post_payment(payment: Payment, *, user=None) -> Payment:
    """Record that the money moved. All of it, or none of it.

    Order matters:

    1. **The lifecycle guard**, so a second post is refused before anything is
       written and the operator hears "already posted" rather than seeing the
       ledger silently double.
    2. **The allocations**, checked against the amount. A draft that was
       allocated and then edited down to less than it was applied to must not
       post — the money would be settling bills it cannot cover.
    3. **The two ledger rows**, party-tagged, balanced and asserted.

    Nothing here touches stock: money moving is not goods moving.
    """
    payment.assert_transition(DocumentStatus.POSTED)
    payment._assert_shape()
    assert_allocations_within_payment(payment)

    gl_lines = build_payment_gl(payment)
    assert_gl_balances(gl_lines, payment)
    post_entries(payment, _party_tagged(gl_lines, payment), payment.posting_date, user=user)

    payment.mark_posted(user=user)
    payment.save()
    return payment


@transaction.atomic
def cancel_payment(payment: Payment, *, user=None, reason: str = "") -> Payment:
    """Reverse the two rows this payment wrote, and stop it settling anything.

    Cancelling writes mirror rows into the ledger; it never touches the
    originals (CLAUDE.md §3). The allocation rows are deliberately **left where
    they are** — they are the record of what the money had been applied to, and
    :meth:`~apps.payments.models.PaymentQuerySet.live` already excludes a
    cancelled payment from every figure that counts, so the invoices go back to
    being open without anything having to be deleted.

    A cheque whose fate the bank has already decided cannot be cancelled here:
    there is a second document with its own entries hanging off this one, and
    reversing this one alone would leave those entries pointing at nothing.
    Cancel the cheque event first.
    """
    payment.assert_transition(DocumentStatus.CANCELLED)

    settled = payment.settled_event()
    if settled is not None:
        raise ChequeStateError(
            f"{payment.code} cannot be cancelled: {settled.code} has already recorded that the "
            f"cheque {settled.get_kind_display().lower()}. Cancel {settled.code} first, which "
            f"reverses its entries, and then cancel this."
        )

    reverse_entries(payment, user=user)
    payment.mark_cancelled(user=user, reason=reason)
    payment.save()
    return payment


@transaction.atomic
def amend_payment(payment: Payment, *, user=None) -> Payment:
    """Clone a CANCELLED payment into a new DRAFT, allocations and all.

    The allocations are carried across because they are what the operator meant:
    a payment cancelled to correct its date or its amount was still against the
    same bills. If the amount comes down past what they add up to,
    :func:`post_payment` refuses it and says by how much.
    """
    amendment = payment.build_amendment(user=user)
    PaymentAllocation.objects.bulk_create(
        [
            PaymentAllocation(
                payment=amendment,
                invoice_type=allocation.invoice_type,
                invoice_id=allocation.invoice_id,
                amount_paisa=allocation.amount_paisa,
                created_by=user,
                updated_by=user,
            )
            for allocation in payment.allocations.all()
        ]
    )
    return amendment


# ===========================================================================
# Allocation
# ===========================================================================
class Allocation(NamedTuple):
    """One payment applied to one document.

    The shape :func:`allocations_for` returns, and the whole of what purchasing
    and sales know about a payment — see
    :func:`apps.purchasing.services.payment_allocations`, which has been waiting
    for this function to exist.
    """

    code: str
    amount_paisa: int


def allocations_for(document):
    """Every **live** payment allocated against this document.

    The seam purchasing and sales read through, and the reason
    :attr:`apps.sales.models.SalesInvoice.paid_paisa` is a property rather than
    a column (CLAUDE.md §6).

    "Live" is doing real work here. A cancelled payment is excluded, and so is
    one whose cheque bounced — the bill went back to being unpaid the moment the
    bank sent the cheque back, and an invoice that still counted it would be
    quietly written off.
    """
    allocations = (
        PaymentAllocation.objects.live()
        .for_document(document)
        .select_related("payment")
        .order_by("payment__posting_date", "payment_id")
    )
    return [
        Allocation(code=allocation.payment.code, amount_paisa=allocation.amount_paisa)
        for allocation in allocations
    ]


def assert_allocations_within_payment(payment) -> None:
    """Raise unless this payment's allocations fit inside it.

    The remainder is fine — that is an on-account balance. What is refused is
    applying more money than arrived.
    """
    allocated = payment.allocated_paisa
    if allocated > payment.amount_paisa:
        raise OverAllocated(
            subject=f"{payment.code}",
            limit_paisa=payment.amount_paisa,
            requested_paisa=allocated,
            hint="Reduce an allocation, or raise the payment.",
        )


def assert_allocatable(payment, document) -> int:
    """Raise unless this money may be applied to this document at all.

    Four things, all of which produce a link no report could make sense of:

    * a document type payments has never heard of;
    * a document that is not POSTED — a draft owes nothing yet, and a cancelled
      one has been reversed out of the ledger;
    * a document belonging to somebody else. Money from one shop settling
      another shop's bill is the mistake this catches, and it is a common one:
      two shops on the same beat with similar names;
    * a document facing the same way as the money. A receipt settles a **bill**
      and a refund settles a **credit note**; applying a receipt to a credit
      note would add two figures that already point the same way, and the
      client's ageing would stop adding up to their ledger balance.

    Returns the document's ledger balance, party-signed, because the caller
    needs it next and reading it twice is a second query for the same answer.
    """
    spec = allocatable_spec(type(document).__name__)

    if document.status != DocumentStatus.POSTED:
        raise NotAllocatable(
            f"{document.code} is {document.status}. Money is allocated to documents that are "
            f"posted — a draft owes nothing yet, and a cancelled one has been reversed out of "
            f"the ledger."
        )

    document_party_id = getattr(document, spec.party_field)
    if spec.party_type != payment.party_type or document_party_id != payment.party_id:
        holder = party_model(spec.party_type).objects.filter(pk=document_party_id).first()
        raise NotAllocatable(
            f"{document.code} belongs to {holder.name if holder else 'another party'}, not to "
            f"{payment.party_name}. Money from one party cannot settle another's bill."
        )

    balance = document_balance_paisa(document)
    receiving = payment.direction == PaymentDirection.RECEIVE
    if receiving and balance <= 0:
        raise NotAllocatable(
            f"{document.code} is not a bill — it puts {fmt(abs(balance))} in "
            f"{payment.party_name}'s favour. Money coming in settles what they owe; a credit "
            f"note is settled by paying it out."
        )
    if not receiving and balance >= 0:
        raise NotAllocatable(
            f"{document.code} is a bill for {fmt(balance)}, not something owed back to "
            f"{payment.party_name}. Money going out settles a credit note or an overpayment."
        )
    return balance


def assert_document_can_take(payment, document, amount_paisa: int, balance_paisa=None) -> None:
    """Raise unless ``amount_paisa`` still fits on this document.

    Counts what every other live payment has already put on it, so three
    part-payments cannot quietly add up to more than the bill. This payment's
    own existing allocation to the document is excluded from that total —
    otherwise re-allocating the same receipt to the same invoice would refuse
    itself on the second pass.
    """
    already = document_allocated_paisa(document)
    mine = (
        PaymentAllocation.objects.filter(
            payment=payment,
            invoice_type=type(document).__name__,
            invoice_id=document.pk,
        )
        .values_list("amount_paisa", flat=True)
        .first()
        or 0
    )
    balance = document_balance_paisa(document) if balance_paisa is None else balance_paisa
    others = already - mine
    room = abs(balance) - others

    if amount_paisa > room:
        raise OverAllocated(
            subject=f"{document.code}",
            limit_paisa=room,
            requested_paisa=amount_paisa,
            hint=(
                f"{fmt(others)} is already allocated to it by other payments."
                if others
                else "It is already settled."
            ),
        )


@transaction.atomic
def allocate_payment(payment: Payment, allocations, *, replace: bool = True, user=None) -> Payment:
    """Apply a payment to documents. ``allocations`` is ``[(document, paisa), …]``.

    **Replaces** the payment's whole allocation set by default, because that is
    what the screens submit: the recovery workspace posts an amount against
    every open invoice at once, and a row left blank means "nothing on this
    one", not "leave whatever was there". Pass ``replace=False`` to add to what
    is already there.

    Zero and negative amounts are dropped rather than stored — a row nobody
    typed into is not an allocation of nothing, it is an absence.

    Every allocation is checked twice: it must fit inside the payment, and it
    must fit on the document once every other live payment has had its share.
    Neither check has anything to say about the **remainder**, which stays on
    account and is shown as such.

    Allowed on a DRAFT and on a POSTED payment, and refused on a CANCELLED one.
    That is not an oversight in the immutability rule — an allocation writes no
    ledger row (see :class:`~apps.payments.models.PaymentAllocation`), so
    nothing about the posted document changes when one moves.
    """
    if payment.status == DocumentStatus.CANCELLED:
        raise NotAllocatable(
            f"{payment.code} is CANCELLED; its entries have been reversed and there is no money "
            f"left on it to allocate."
        )

    wanted: list[tuple[object, int]] = []
    for document, amount_paisa in allocations:
        amount_paisa = _as_paisa(amount_paisa, document)
        if amount_paisa <= 0:
            continue
        balance = assert_allocatable(payment, document)
        assert_document_can_take(payment, document, amount_paisa, balance_paisa=balance)
        wanted.append((document, amount_paisa))

    if replace:
        keep = {(type(document).__name__, document.pk) for document, _ in wanted}
        stale = [
            allocation
            for allocation in payment.allocations.all()
            if (allocation.invoice_type, allocation.invoice_id) not in keep
        ]
        for allocation in stale:
            allocation.delete()

    total = sum(amount for _document, amount in wanted)
    if not replace:
        # Adding rather than replacing, so what is already there counts towards
        # the payment's total — except on the documents this call overwrites,
        # whose new figures are already in `total`.
        overwritten = {(type(document).__name__, document.pk) for document, _ in wanted}
        total += sum(
            allocation.amount_paisa
            for allocation in payment.allocations.all()
            if (allocation.invoice_type, allocation.invoice_id) not in overwritten
        )
    if total > payment.amount_paisa:
        raise OverAllocated(
            subject=payment.code,
            limit_paisa=payment.amount_paisa,
            requested_paisa=total,
            hint="The rest of a payment stays on account; more than all of it cannot.",
        )

    for document, amount_paisa in wanted:
        PaymentAllocation.objects.update_or_create(
            payment=payment,
            invoice_type=type(document).__name__,
            invoice_id=document.pk,
            defaults={"amount_paisa": amount_paisa, "updated_by": user},
            create_defaults={
                "amount_paisa": amount_paisa,
                "created_by": user,
                "updated_by": user,
            },
        )
    return payment


@transaction.atomic
def unallocate(payment: Payment) -> Payment:
    """Take a payment off every bill. The money goes back on account."""
    payment.allocations.all().delete()
    return payment


@transaction.atomic
def auto_allocate(payment: Payment, *, user=None) -> Payment:
    """Spend a payment's unallocated remainder on its party's oldest bills first.

    The "apply to oldest" button, and the fairest default there is: an
    accountant applying money by hand applies it to the oldest thing, because
    that is what keeps the ageing sheet honest.

    Money coming in goes onto the oldest **bills**; money going out settles the
    oldest **credit notes**. Those are the only two things it can do — see
    :func:`assert_allocatable` — so the queue it walks depends on the direction.

    Adds to whatever is already allocated rather than replacing it, and stops
    when the money runs out — anything left over stays on account.
    """
    remaining = payment.unallocated_paisa
    if remaining <= 0:
        return payment

    items, credits = open_items(payment.party_type, payment.party_id, as_of=payment.posting_date)
    queue = items if payment.direction == PaymentDirection.RECEIVE else credits

    additions: list[tuple[object, int]] = []
    for entry in queue:
        if remaining <= 0:
            break
        if entry.voucher_type not in ALLOCATABLE_DOCUMENTS:
            continue
        document = allocatable_model(entry.voucher_type).objects.filter(pk=entry.voucher_id).first()
        if document is None or document.status != DocumentStatus.POSTED:
            continue
        take = min(document_open_paisa(document), remaining)
        if take <= 0:
            continue
        additions.append((document, take))
        remaining -= take

    if additions:
        allocate_payment(payment, additions, replace=False, user=user)
    return payment


def _as_paisa(value, document) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidPayment(
            f"The amount allocated to {getattr(document, 'code', document)} must be whole paisa "
            f"as an int, got {type(value).__name__}: {value!r}. Run operator input through "
            f"apps.core.money.to_paisa first."
        )
    return value


# ===========================================================================
# The cheque lifecycle
# ===========================================================================
def assert_settleable(payment) -> None:
    """Raise unless this cheque is in a state the bank can act on."""
    if not payment.is_cheque:
        raise ChequeStateError(
            f"{payment.code} was taken in {payment.get_mode_display().lower()}, not by cheque. "
            f"There is nothing to clear or bounce."
        )
    if payment.status != DocumentStatus.POSTED:
        raise ChequeStateError(
            f"{payment.code} is {payment.status}. A cheque is cleared or bounced after the "
            f"payment that took it has been posted."
        )
    settled = payment.settled_event()
    if settled is not None:
        raise ChequeStateError(
            f"{payment.code} has already been settled by {settled.code}: the cheque "
            f"{settled.get_kind_display().lower()} on {settled.posting_date}. Cancel that event "
            f"if it was recorded in error."
        )


@transaction.atomic
def create_cheque_event(
    payment: Payment,
    *,
    kind: str,
    posting_date: date | None = None,
    remarks: str = "",
    user=None,
) -> ChequeEvent:
    """A new DRAFT cheque event with a freshly allocated code.

    ``posting_date`` defaults to the date written on the cheque, which is the
    earliest day it could have been banked and the day it usually was.
    """
    assert_settleable(payment)
    return ChequeEvent.objects.create(
        code=get_next_code(
            CHEQUE_EVENT_PREFIX, fiscal_year_of(posting_date or payment.cheque_date)
        ),
        payment=payment,
        kind=kind,
        posting_date=posting_date or payment.cheque_date,
        remarks=remarks,
        created_by=user,
        updated_by=user,
    )


@transaction.atomic
def post_cheque_event(event: ChequeEvent, *, user=None) -> ChequeEvent:
    """Write what the bank did into the ledger. All of it, or none of it."""
    event.assert_transition(DocumentStatus.POSTED)
    assert_settleable(event.payment)

    if event.posting_date < event.payment.posting_date:
        raise ChequeStateError(
            f"{event.code} is dated {event.posting_date}, before {event.payment.code} took the "
            f"cheque on {event.payment.posting_date}. A cheque cannot clear before it is taken."
        )

    gl_lines = build_cheque_event_gl(event)
    assert_gl_balances(gl_lines, event)
    post_entries(event, _party_tagged(gl_lines, event.payment), event.posting_date, user=user)

    event.mark_posted(user=user)
    event.save()
    return event


@transaction.atomic
def clear_cheque(
    payment: Payment, *, posting_date: date | None = None, user=None, remarks: str = ""
) -> ChequeEvent:
    """The bank took it. Move the money from Cheques in Hand to Bank.

    The small separate action the whole cheque design exists for. Until this
    runs, the cheque is a piece of paper in a drawer and the bank balance does
    not know about it — which is the only honest way to run a business where
    most of what comes in is post-dated.
    """
    event = create_cheque_event(
        payment,
        kind=ChequeEventKind.CLEARED,
        posting_date=posting_date,
        remarks=remarks,
        user=user,
    )
    return post_cheque_event(event, user=user)


@transaction.atomic
def bounce_cheque(
    payment: Payment, *, posting_date: date | None = None, user=None, remarks: str = ""
) -> ChequeEvent:
    """The bank sent it back. Put the debt back and flag the shop.

    Writes the payment's own posting the other way round — the receivable
    returns, Cheques in Hand is emptied of a cheque that turned out to be
    nothing — and, because
    :meth:`~apps.payments.models.PaymentQuerySet.live` excludes a bounced
    payment, every invoice it had been allocated to goes back to being open in
    the same instant. Nothing has to be unallocated by hand.

    The shop is **flagged by this event existing**, not by a column somebody has
    to remember to set: see
    :attr:`apps.payments.recovery.ClientRecovery.is_flagged`. The workspace puts
    a flagged shop in the alarm colour, which is what somebody needs to see
    before agreeing to take another cheque from them.

    The payment itself is not touched. It is a true record that a cheque was
    taken on a day, and a POSTED document is immutable (CLAUDE.md §5).
    """
    event = create_cheque_event(
        payment,
        kind=ChequeEventKind.BOUNCED,
        posting_date=posting_date,
        remarks=remarks,
        user=user,
    )
    return post_cheque_event(event, user=user)


@transaction.atomic
def cancel_cheque_event(event: ChequeEvent, *, user=None, reason: str = "") -> ChequeEvent:
    """Reverse a clearing or a bounce that was recorded in error.

    Writes mirror rows and never touches the originals (CLAUDE.md §3). Once it
    is cancelled the cheque is back to PENDING and the payment is live money
    again — which for a cancelled bounce is exactly right: the cheque was fine
    all along.
    """
    event.assert_transition(DocumentStatus.CANCELLED)
    reverse_entries(event, user=user)
    event.mark_cancelled(user=user, reason=reason)
    event.save()
    return event


@transaction.atomic
def amend_cheque_event(event: ChequeEvent, *, user=None) -> ChequeEvent:
    """Clone a CANCELLED cheque event into a new DRAFT.

    For the ordinary correction: a clearing entered on the wrong date. Amending
    keeps the chain — ``CHQ-2026-000012`` becomes ``CHQ-2026-000012-1`` — so the
    reversal and the replacement can be read together.
    """
    return event.build_amendment(user=user)


# ===========================================================================
# Reading
# ===========================================================================
def attach_parties(payments):
    """Bulk-load the party behind a page of payments. One query per type.

    The soft ``(type, id)`` link has no ``select_related``, so a list of fifty
    receipts would otherwise be fifty queries for fifty shop names. Sets the
    same cache :attr:`~apps.payments.models.Payment.party` reads.
    """
    payments = list(payments)
    by_type: dict[str, set[int]] = {}
    for payment in payments:
        by_type.setdefault(payment.party_type, set()).add(payment.party_id)

    loaded: dict[tuple[str, int], object] = {}
    for party_type, ids in by_type.items():
        for party in party_model(party_type).objects.filter(pk__in=ids):
            loaded[(party_type, party.pk)] = party

    for payment in payments:
        payment._party = loaded.get((payment.party_type, payment.party_id))
    return payments


def allocation_rows(payment):
    """A payment's allocations with their documents resolved, for the screen."""
    allocations = list(payment.allocations.all())
    by_type: dict[str, list[int]] = {}
    for allocation in allocations:
        by_type.setdefault(allocation.invoice_type, []).append(allocation.invoice_id)

    loaded: dict[tuple[str, int], object] = {}
    for invoice_type, ids in by_type.items():
        for document in allocatable_model(invoice_type).objects.filter(pk__in=ids):
            loaded[(invoice_type, document.pk)] = document

    for allocation in allocations:
        allocation._document = loaded.get((allocation.invoice_type, allocation.invoice_id))
    return allocations


def cheque_summary(payment) -> dict:
    """Everything the payment screen shows about the cheque, in one dict."""
    if not payment.is_cheque:
        return {}
    event = payment.settled_event()
    return {
        "status": payment.cheque_status,
        "label": payment.cheque_status_label,
        "is_pending": payment.cheque_status == ChequeStatus.PENDING,
        "is_cleared": payment.cheque_status == ChequeStatus.CLEARED,
        "is_bounced": payment.cheque_status == ChequeStatus.BOUNCED,
        "event": event,
        "may_settle": payment.status == DocumentStatus.POSTED and event is None,
    }


__all__ = [
    "Allocation",
    "allocate_payment",
    "allocation_rows",
    "allocations_for",
    "amend_cheque_event",
    "amend_payment",
    "assert_allocatable",
    "assert_allocations_within_payment",
    "assert_document_can_take",
    "assert_settleable",
    "attach_parties",
    "auto_allocate",
    "bounce_cheque",
    "build_cheque_event_gl",
    "build_payment_gl",
    "cancel_cheque_event",
    "cancel_payment",
    "cheque_summary",
    "clear_cheque",
    "create_cheque_event",
    "create_payment",
    "fiscal_year_of",
    "money_account_code",
    "party_account_code",
    "post_cheque_event",
    "post_payment",
    "prefix_for",
    "unallocate",
]
