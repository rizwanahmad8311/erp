"""Purchase invoice and purchase return: the arithmetic, and the two postings.

Everything that writes a ledger or stock row for a purchase document is in this
module, wrapped in ``transaction.atomic()`` (CLAUDE.md §4). Views and admin
actions call these functions; they never build a ledger row themselves.

The rounding rule
-----------------
The line arithmetic lives in :mod:`apps.masters.pricing` and is re-exported
here, because it is not a purchasing rule — sales asks the identical question in
the other direction. In one sentence:

    **The money the supplier billed is exact. The per-piece rate is derived.**

What purchasing adds is the consequence: because ``rate_paisa`` is a rounded
derivation, the stock receipt is posted at the line's **value**, and Inventory is
debited that same value. The two ledgers therefore agree to the paisa on every
line, always, and the difference between ``qty_base * rate_paisa`` and
``amount_paisa`` never reaches either of them.

Every header total is an exact integer sum of the lines. Nothing rounds at
header level, so ``header == sum(lines)`` is arithmetic, not a tolerance.
"""

from __future__ import annotations

from datetime import date
from typing import NamedTuple

from django.db import transaction

from apps.accounting import chart as coa
from apps.accounting.enums import PartyType
from apps.accounting.posting import (
    GLLine,
    accounts_by_code,
    assert_gl_balances,
    assert_inventory_matches_stock,
    drop_zero_lines,
)
from apps.accounting.refs import PartyRef
from apps.accounting.services import (
    post_entries,
    post_stock,
    reverse_entries,
    reverse_stock,
    stock_balance,
    valuation_rate,
)
from apps.accounting.valuation import Position
from apps.core.enums import DocumentStatus
from apps.core.money import Money, fmt
from apps.core.services import get_next_code

# The line arithmetic is masters', not purchasing's: every argument to it is an
# item or a number, and sales asks the identical question in the other
# direction. Re-exported below so callers of this module keep working.
from apps.masters.pricing import (
    LineAmounts,
    apply_line_amounts,
    compute_line,
    entry_rate_paisa,
    update_line,
)

from .enums import PURCHASE_INVOICE_PREFIX, PURCHASE_RETURN_PREFIX
from .exceptions import PaymentAllocated
from .models import (
    PurchaseInvoice,
    PurchaseInvoiceLine,
    PurchaseReturn,
    PurchaseReturnLine,
)


# ===========================================================================
# Header totals
# ===========================================================================
def recalculate_totals(document, *, save: bool = True):
    """Recompute the four header figures from the lines.

    Every one is an exact integer sum — nothing rounds here, so the header can
    never drift from the lines by a paisa. These fields are a display
    convenience and the source of truth for nothing (CLAUDE.md §6); they are
    recomputed rather than adjusted, always.
    """
    subtotal = Money.zero()
    discount = Money.zero()
    tax = Money.zero()

    for line in document.lines.all():
        subtotal += Money(line.amount_paisa)
        discount += Money(line.discount_paisa)
        tax += Money(line.tax_paisa)

    document.subtotal_paisa = subtotal.paisa
    document.discount_paisa = discount.paisa
    document.tax_paisa = tax.paisa
    document.total_paisa = (subtotal - discount + tax).paisa

    if save:
        document.save(
            update_fields=[
                "subtotal_paisa",
                "discount_paisa",
                "tax_paisa",
                "total_paisa",
                "updated_at",
            ]
        )
    return document


# ===========================================================================
# The general ledger side
# ===========================================================================


def build_invoice_gl(invoice, *, subtotal_paisa=None, discount_paisa=None, tax_paisa=None):
    """The general ledger a purchase invoice posts.

        Dr Inventory           the goods, at what the bill says they cost
        Dr Tax Payable         input tax, which reduces what we owe the government
        Cr Discount Received   the supplier's discount, taken as income
        Cr Accounts Payable    what the supplier is owed, tagged with the vendor

    Inventory is debited the **gross** line amount and the discount is credited
    to income, because that is how this installation treats a supplier discount:
    as something earned, not as a reduction in what the goods are carried at.
    The consequence is deliberate and worth knowing — stock is valued at list
    price, so the margin shows up on the purchase rather than on the sale. A
    business that wants the discount to reduce inventory cost instead would
    debit Inventory the net and drop the Discount Received line; both balance,
    and this one is what was asked for.

    Balances exactly, by construction and with no rounding anywhere:
    ``subtotal + tax == total + discount``, since ``total`` is defined as
    ``subtotal - discount + tax``.

    The amounts default to the header's, and are accepted as arguments so the
    entry screen can preview a document that has not been saved yet.
    """
    subtotal = Money(invoice.subtotal_paisa if subtotal_paisa is None else subtotal_paisa)
    discount = Money(invoice.discount_paisa if discount_paisa is None else discount_paisa)
    tax = Money(invoice.tax_paisa if tax_paisa is None else tax_paisa)
    total = subtotal - discount + tax

    account = accounts_by_code(
        coa.INVENTORY, coa.TAX_PAYABLE, coa.DISCOUNT_RECEIVED, coa.ACCOUNTS_PAYABLE
    )

    gl = [GLLine(account[coa.INVENTORY], subtotal.paisa, 0, "Goods received")]
    if tax:
        gl.append(GLLine(account[coa.TAX_PAYABLE], tax.paisa, 0, "Input tax"))
    if discount:
        gl.append(GLLine(account[coa.DISCOUNT_RECEIVED], 0, discount.paisa, "Supplier discount"))
    gl.append(
        GLLine(account[coa.ACCOUNTS_PAYABLE], 0, total.paisa, f"Payable to {invoice.vendor.name}")
    )
    return drop_zero_lines(gl)


def build_return_gl(
    document,
    *,
    cost_released_paisa: int,
    subtotal_paisa=None,
    discount_paisa=None,
    tax_paisa=None,
):
    """The general ledger a purchase return posts — the invoice's, mirrored.

        Dr Accounts Payable    what the supplier now credits back
        Dr Discount Received   the discount, given back
        Cr Inventory           the goods, at what they are carried at
        Cr Tax Payable         the input tax, reversed

    One line is **not** a mirror, and cannot be. Stock leaves at the moving
    weighted average for that ``(item, warehouse)``, not at the rate on this
    credit note — that rule belongs to
    :func:`apps.accounting.services.post_stock` and exists so a document's own
    idea of price can never re-value inventory. When the average has moved since
    the goods came in, the value released is not the value being credited, and
    the difference is a real gain or loss that has to land somewhere visible:

        Cr Other Income            credited more than the goods are carried at
        Dr Miscellaneous Expenses  credited less

    In the ordinary case — a return against stock that came in at this price and
    has not been averaged with anything else — that difference is exactly zero
    and no such line is written.
    """
    subtotal = Money(document.subtotal_paisa if subtotal_paisa is None else subtotal_paisa)
    discount = Money(document.discount_paisa if discount_paisa is None else discount_paisa)
    tax = Money(document.tax_paisa if tax_paisa is None else tax_paisa)
    total = subtotal - discount + tax
    cost = Money(cost_released_paisa)

    account = accounts_by_code(
        coa.ACCOUNTS_PAYABLE,
        coa.DISCOUNT_RECEIVED,
        coa.INVENTORY,
        coa.TAX_PAYABLE,
        coa.OTHER_INCOME,
        coa.MISCELLANEOUS_EXPENSES,
    )

    gl = [
        GLLine(account[coa.ACCOUNTS_PAYABLE], total.paisa, 0, f"Credit from {document.vendor.name}")
    ]
    if discount:
        gl.append(GLLine(account[coa.DISCOUNT_RECEIVED], discount.paisa, 0, "Discount given back"))
    gl.append(GLLine(account[coa.INVENTORY], 0, cost.paisa, "Goods returned, at cost"))
    if tax:
        gl.append(GLLine(account[coa.TAX_PAYABLE], 0, tax.paisa, "Input tax reversed"))

    # What the supplier credits, against what the goods are carried at.
    difference = subtotal - cost
    if difference.paisa > 0:
        gl.append(GLLine(account[coa.OTHER_INCOME], 0, difference.paisa, "Gain on purchase return"))
    elif difference.paisa < 0:
        gl.append(
            GLLine(
                account[coa.MISCELLANEOUS_EXPENSES],
                -difference.paisa,
                0,
                "Loss on purchase return",
            )
        )

    return drop_zero_lines(gl)


# ===========================================================================
# Purchase invoice
# ===========================================================================
@transaction.atomic
def post_purchase_invoice(invoice: PurchaseInvoice, *, user=None) -> PurchaseInvoice:
    """Receive the goods and record what is owed. All of it, or none of it.

    Stock first, then the ledger, then the status — all inside one
    ``atomic()``, so a failure anywhere leaves the invoice a DRAFT with nothing
    written (CLAUDE.md §4).

    Each line is received at its **value**, not at its rate: ``value_paisa`` is
    the line amount the supplier billed, so what Inventory is debited and what
    the stock ledger holds are the same number to the paisa. See the module
    docstring for why that is not the same as ``qty_base * rate_paisa``.
    """
    invoice.assert_transition(DocumentStatus.POSTED)
    invoice.assert_has_lines()

    recalculate_totals(invoice)
    lines = list(invoice.lines.select_related("item"))

    movements = post_stock(
        invoice,
        [
            {
                "item": line.item,
                "warehouse": invoice.warehouse,
                "qty_base": line.qty_base,
                "value_paisa": line.amount_paisa,
            }
            for line in lines
        ],
        invoice.posting_date,
        user=user,
    )

    gl_lines = build_invoice_gl(invoice)
    assert_gl_balances(gl_lines, invoice)
    assert_inventory_matches_stock(gl_lines, movements, invoice)

    party = PartyRef(PartyType.VENDOR, invoice.vendor_id)
    post_entries(
        invoice,
        [
            gl.as_entry(party if gl.account.code == coa.ACCOUNTS_PAYABLE else None)
            for gl in gl_lines
        ],
        invoice.posting_date,
        user=user,
    )

    invoice.mark_posted(user=user)
    invoice.save()
    return invoice


@transaction.atomic
def cancel_purchase_invoice(
    invoice: PurchaseInvoice, *, user=None, reason: str = ""
) -> PurchaseInvoice:
    """Reverse everything this invoice wrote. Refuses if it has been paid.

    Cancelling writes mirror rows into both ledgers; it never touches the
    originals (CLAUDE.md §3). What it will not do is cancel an invoice that
    money has been allocated against — the payment is a different document with
    its own rows, and reversing this one would leave that payment sitting
    against a supplier balance with no invoice under it.
    """
    invoice.assert_transition(DocumentStatus.CANCELLED)
    assert_not_paid(invoice)

    reverse_stock(invoice, user=user)
    reverse_entries(invoice, user=user)

    invoice.mark_cancelled(user=user, reason=reason)
    invoice.save()
    return invoice


@transaction.atomic
def amend_purchase_invoice(invoice: PurchaseInvoice, *, user=None) -> PurchaseInvoice:
    """Clone a CANCELLED invoice into a new DRAFT, lines and all.

    The base class refuses to amend anything that is not CANCELLED: amending a
    POSTED document would double-count it, since nothing has been reversed yet
    (CLAUDE.md §5). The new code suffixes the *root* of the chain, so amending
    twice gives ``-1`` then ``-2`` rather than ``-1-1``.
    """
    amendment = invoice.build_amendment(user=user)
    _copy_lines(invoice, amendment, PurchaseInvoiceLine)
    recalculate_totals(amendment)
    return amendment


# ===========================================================================
# Purchase return
# ===========================================================================
@transaction.atomic
def post_purchase_return(document: PurchaseReturn, *, user=None) -> PurchaseReturn:
    """Send the goods back and reduce what is owed. All of it, or none of it.

    Stock leaves **before** the ledger is built, because until it has left
    nobody knows what it was carried at — an issue is valued at the moving
    average, and that figure is what Inventory is credited. See
    :func:`build_return_gl`.
    """
    document.assert_transition(DocumentStatus.POSTED)
    document.assert_has_lines()

    recalculate_totals(document)
    lines = list(document.lines.select_related("item"))

    movements = post_stock(
        document,
        [
            {
                "item": line.item,
                "warehouse": document.warehouse,
                # Negative: stock going out. No rate and no value — the stock
                # ledger values an issue itself, at the average it holds.
                "qty_base": -line.qty_base,
            }
            for line in lines
        ],
        document.posting_date,
        user=user,
    )

    cost_released = -sum(movement.value_paisa for movement in movements)
    gl_lines = build_return_gl(document, cost_released_paisa=cost_released)
    assert_gl_balances(gl_lines, document)
    assert_inventory_matches_stock(gl_lines, movements, document)

    party = PartyRef(PartyType.VENDOR, document.vendor_id)
    post_entries(
        document,
        [
            gl.as_entry(party if gl.account.code == coa.ACCOUNTS_PAYABLE else None)
            for gl in gl_lines
        ],
        document.posting_date,
        user=user,
    )

    document.mark_posted(user=user)
    document.save()
    return document


@transaction.atomic
def cancel_purchase_return(
    document: PurchaseReturn, *, user=None, reason: str = ""
) -> PurchaseReturn:
    """Reverse everything this return wrote. Refuses if it has been settled."""
    document.assert_transition(DocumentStatus.CANCELLED)
    assert_not_paid(document)

    reverse_stock(document, user=user)
    reverse_entries(document, user=user)

    document.mark_cancelled(user=user, reason=reason)
    document.save()
    return document


@transaction.atomic
def amend_purchase_return(document: PurchaseReturn, *, user=None) -> PurchaseReturn:
    """Clone a CANCELLED return into a new DRAFT, lines and all."""
    amendment = document.build_amendment(user=user)
    _copy_lines(document, amendment, PurchaseReturnLine)
    recalculate_totals(amendment)
    return amendment


def preview_return_cost_paisa(document) -> int:
    """What a return's stock issue *would* be valued at, for the GL preview.

    An estimate, and labelled as one wherever it is shown. The figure that gets
    posted is computed by :func:`~apps.accounting.services.post_stock` under the
    write lock; this walks the same :class:`~apps.accounting.valuation.Position`
    arithmetic outside it, so a document posted a moment later will normally
    agree exactly, and will differ if someone else moved the same stock in
    between. Nothing is written here.
    """
    positions: dict[tuple[int, int], Position] = {}
    released = Money.zero()

    for line in document.lines.select_related("item"):
        key = (line.item_id, document.warehouse_id)
        if key not in positions:
            qty_base, value_paisa = stock_balance(
                line.item, document.warehouse, document.posting_date
            )
            positions[key] = Position(qty_base=qty_base, value_paisa=value_paisa)

        fallback = valuation_rate(line.item, document.warehouse, document.posting_date)
        movement = positions[key].issue(line.qty_base, fallback_rate_paisa=fallback)
        positions[key] = positions[key].apply(movement)
        released += Money(-movement.value_paisa)

    return released.paisa


# ===========================================================================
# Payments
# ===========================================================================
class Allocation(NamedTuple):
    """One payment applied to one purchase document.

    The shape :func:`payment_allocations` returns and the only thing this app
    knows about a payment: what it is called, and how much of it landed here.
    """

    code: str
    amount_paisa: int


def payment_allocations(document) -> list[Allocation]:
    """Every payment allocated against this document.

    The seam between purchasing and payments, and the reason
    :attr:`~apps.purchasing.models.PurchaseInvoice.paid_paisa` is a property
    rather than a column (CLAUDE.md §6).

    ``apps.payments`` has no models yet, so this returns an empty list today —
    by *asking* and finding nothing, not by being hardcoded. The contract it
    asks for is one function::

        # apps/payments/services.py
        def allocations_for(document) -> Iterable[Allocation]: ...

    The day that exists, ``paid_paisa`` starts returning real figures and
    cancellation starts refusing paid invoices, with no change in this app.
    """
    try:
        from apps.payments import services as payments_services
    except ImportError:
        return []

    resolver = getattr(payments_services, "allocations_for", None)
    if resolver is None:
        return []
    return [Allocation(str(item.code), int(item.amount_paisa)) for item in resolver(document)]


def assert_not_paid(document) -> None:
    """Raise unless nothing has been allocated against this document.

    The message names the payments, because "unallocate the payment first" is
    only actionable if you can see which payment.
    """
    allocations = payment_allocations(document)
    if not allocations:
        return

    named = ", ".join(f"{a.code} ({fmt(a.amount_paisa)})" for a in allocations)
    total = sum(a.amount_paisa for a in allocations)
    raise PaymentAllocated(
        f"{type(document).__name__} {document.code} cannot be cancelled: {fmt(total)} is "
        f"allocated against it by {named}. Unallocate {'them' if len(allocations) > 1 else 'it'} "
        f"first, then cancel.",
        payments=allocations,
    )


# ===========================================================================
# Draft creation
# ===========================================================================
@transaction.atomic
def create_purchase_invoice(*, vendor, warehouse, posting_date: date, **fields) -> PurchaseInvoice:
    """A new DRAFT invoice with a freshly allocated code.

    The code comes from :func:`apps.core.services.get_next_code` inside this
    same transaction, so a failed save does not burn a number (CLAUDE.md §5).
    """
    return PurchaseInvoice.objects.create(
        code=get_next_code(PURCHASE_INVOICE_PREFIX, fiscal_year_of(posting_date)),
        vendor=vendor,
        warehouse=warehouse,
        posting_date=posting_date,
        **fields,
    )


@transaction.atomic
def create_purchase_return(*, vendor, warehouse, posting_date: date, **fields) -> PurchaseReturn:
    """A new DRAFT return with a freshly allocated code."""
    return PurchaseReturn.objects.create(
        code=get_next_code(PURCHASE_RETURN_PREFIX, fiscal_year_of(posting_date)),
        vendor=vendor,
        warehouse=warehouse,
        posting_date=posting_date,
        **fields,
    )


def fiscal_year_of(posting_date: date) -> int:
    """Which numbering year a document belongs to.

    The calendar year of the posting date. This installation has no fiscal-year
    policy yet; when it gets one — a July-to-June year is the usual Pakistani
    choice — this is the single function that changes, and every document
    prefix follows it at once.
    """
    return posting_date.year


# ===========================================================================
# Internals
# ===========================================================================
def _copy_lines(source, target, line_model) -> None:
    """Copy every line from one document onto another, amounts and all.

    The amounts are carried across rather than recomputed. An amendment starts
    as an exact copy of what was cancelled; the operator then changes the one
    thing that was wrong, and that change goes back through
    :func:`compute_line`. Recomputing here would silently re-rate the whole
    document against today's item defaults.
    """
    line_model.objects.bulk_create(
        [
            line_model(
                document=target,
                item=line.item,
                qty_input=line.qty_input,
                unit_input=line.unit_input,
                qty_base=line.qty_base,
                rate_paisa=line.rate_paisa,
                discount_paisa=line.discount_paisa,
                tax_paisa=line.tax_paisa,
                amount_paisa=line.amount_paisa,
            )
            for line in source.lines.select_related("item")
        ]
    )


__all__ = [
    "Allocation",
    "GLLine",
    "LineAmounts",
    "amend_purchase_invoice",
    "amend_purchase_return",
    "apply_line_amounts",
    "assert_gl_balances",
    "assert_not_paid",
    "build_invoice_gl",
    "build_return_gl",
    "cancel_purchase_invoice",
    "cancel_purchase_return",
    "compute_line",
    "create_purchase_invoice",
    "create_purchase_return",
    "entry_rate_paisa",
    "fiscal_year_of",
    "payment_allocations",
    "post_purchase_invoice",
    "post_purchase_return",
    "preview_return_cost_paisa",
    "recalculate_totals",
    "update_line",
]
