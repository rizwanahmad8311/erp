"""Purchase invoice and purchase return: the arithmetic, and the two postings.

Everything that writes a ledger or stock row for a purchase document is in this
module, wrapped in ``transaction.atomic()`` (CLAUDE.md §4). Views and admin
actions call these functions; they never build a ledger row themselves.

The rounding rule
-----------------
A supplier bills by the carton and the stock ledger stores pieces, and those two
facts do not divide into each other. The rule this module keeps, everywhere:

    **The money the supplier billed is exact. The per-piece rate is derived.**

``amount_paisa`` is ``qty_input * rate_input_paisa`` — an integer times an
integer, so there is nothing to round and nothing is rounded. ``rate_paisa`` is
``amount_paisa / qty_base`` put through
:func:`~apps.core.money.round_paisa` once, and it is recorded for the stock card
rather than multiplied back out.

Ten cartons of twelve at Rs 2,400 is 120 pieces at exactly Rs 200 and the two
agree. Ten cartons of twenty-four at Rs 2,500 is 240 pieces at 1041.66... paisa
and **no integer rate multiplies back to Rs 25,000**. When that happens the bill
is right and the rate is a rounded figure — so the stock receipt is posted at the
line's *value*, and Inventory is debited that same value. The two ledgers
therefore agree to the paisa on every line, always, and the difference between
``qty_base * rate_paisa`` and ``amount_paisa`` never reaches either of them.

Every header total is an exact integer sum of the lines. Nothing rounds at
header level, so ``header == sum(lines)`` is arithmetic, not a tolerance.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import NamedTuple

from django.db import transaction

from apps.accounting import chart as coa
from apps.accounting.enums import PartyType
from apps.accounting.exceptions import UnbalancedEntry
from apps.accounting.models import Account
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
from apps.core.money import Money, fmt, round_paisa
from apps.core.services import get_next_code
from apps.masters.enums import Unit
from apps.masters.services import to_base

from .enums import PURCHASE_INVOICE_PREFIX, PURCHASE_RETURN_PREFIX
from .exceptions import InvalidLine, PaymentAllocated
from .models import (
    PurchaseInvoice,
    PurchaseInvoiceLine,
    PurchaseReturn,
    PurchaseReturnLine,
)

#: Basis points in 100%. A tax rate of 1750 bp is 17.5%.
BASIS_POINTS_PER_UNIT = 10_000


# ===========================================================================
# Line arithmetic
# ===========================================================================
@dataclass(frozen=True, slots=True)
class LineAmounts:
    """Everything a purchase line stores, computed from what was typed.

    Immutable, and deliberately not a model instance: this is the arithmetic on
    its own, so it can be tested without a database, a document or a vendor.
    """

    qty_base: int
    rate_paisa: int
    amount_paisa: int
    discount_paisa: int
    tax_paisa: int

    @property
    def net_paisa(self) -> int:
        """After discount, before tax. What the goods actually cost."""
        return self.amount_paisa - self.discount_paisa

    @property
    def total_paisa(self) -> int:
        return self.net_paisa + self.tax_paisa

    @property
    def rate_is_exact(self) -> bool:
        """Whether ``qty_base * rate_paisa`` lands back on ``amount_paisa``.

        False is normal, not a fault — see the module docstring. The entry
        screen shows it so nobody reads a stock card and thinks the bill is
        wrong.
        """
        return self.qty_base * self.rate_paisa == self.amount_paisa

    @property
    def rate_drift_paisa(self) -> int:
        """How far ``qty_base * rate_paisa`` is from the bill. Never posted."""
        return self.qty_base * self.rate_paisa - self.amount_paisa


def compute_line(
    item,
    *,
    qty_input: int,
    unit_input: str = Unit.PIECE,
    rate_input_paisa: int,
    discount_paisa: int = 0,
    tax_rate_bp: int | None = None,
) -> LineAmounts:
    """Turn "10 cartons at Rs 2,400 each" into the five numbers a line stores.

    ``rate_input_paisa`` is the rate **in the unit that was typed** — per carton
    when ``unit_input`` is CARTON. That is what a supplier quotes and what is
    printed on their bill, and it is the only rate the operator ever sees on the
    entry screen.

    The order of operations is the point:

    1. ``amount_paisa = qty_input * rate_input_paisa`` — exact, and the anchor.
       Nothing downstream is allowed to change it.
    2. ``qty_base`` from :func:`apps.masters.services.to_base`, which is the only
       place the carton size is ever applied.
    3. ``rate_paisa`` from the amount, rounded **once**. Derived, for the stock
       card. Where it does not multiply back exactly, the amount is right.
    4. tax on the discounted amount, rounded **once**.

    Doing it the other way round — rate per piece first, amount from
    ``qty_base * rate_paisa`` — puts the supplier's bill out by up to half a
    paisa per piece, which is Rs 1.20 on a 240-piece line that nobody agreed to.

    ``tax_rate_bp`` defaults to the item's own rate. Pass it explicitly only
    when a bill genuinely charges something else.
    """
    qty_input = _as_positive_int(qty_input, "qty_input")
    rate_input_paisa = _as_non_negative_int(rate_input_paisa, "rate_input_paisa")
    discount_paisa = _as_non_negative_int(discount_paisa, "discount_paisa")

    # to_base validates the unit and refuses a fraction of a base unit.
    qty_base = to_base(item, qty_input, unit_input)

    # Two integers. There is nothing here to round, and that is the whole design.
    amount_paisa = qty_input * rate_input_paisa

    if discount_paisa > amount_paisa:
        raise InvalidLine(
            f"Discount of {fmt(discount_paisa)} is more than the line amount of "
            f"{fmt(amount_paisa)}. A line cannot cost less than nothing."
        )

    # The one division on the line, through the one rounding point in the system.
    rate_paisa = round_paisa(Decimal(amount_paisa) / qty_base)

    if tax_rate_bp is None:
        tax_rate_bp = getattr(item, "tax_rate_bp", 0)
    tax_rate_bp = _as_non_negative_int(tax_rate_bp, "tax_rate_bp")
    if tax_rate_bp > BASIS_POINTS_PER_UNIT:
        raise InvalidLine(f"Tax rate of {tax_rate_bp} basis points is over 100%. 1750 is 17.5%.")

    # Money.percent rounds once, through round_paisa. bp/100 is exact in Decimal.
    taxable = Money(amount_paisa - discount_paisa)
    tax_paisa = taxable.percent(Decimal(tax_rate_bp) / 100).paisa

    return LineAmounts(
        qty_base=qty_base,
        rate_paisa=rate_paisa,
        amount_paisa=amount_paisa,
        discount_paisa=discount_paisa,
        tax_paisa=tax_paisa,
    )


def entry_rate_paisa(line) -> int:
    """The rate the operator typed, recovered from a saved line.

    ``amount_paisa`` is ``qty_input * rate_input_paisa``, so this division is
    always exact — there is no rounding here and there must not be. It is what
    lets the entry screen re-show a draft line as "10 cartons @ 2,400" rather
    than as the derived per-piece figure.
    """
    if not line.qty_input:
        return 0
    quotient, remainder = divmod(line.amount_paisa, line.qty_input)
    if remainder:  # pragma: no cover - only reachable if amount was written by hand
        raise InvalidLine(
            f"Line amount {line.amount_paisa} is not a whole multiple of qty_input "
            f"{line.qty_input}; it was not produced by compute_line()."
        )
    return quotient


def apply_line_amounts(line, amounts: LineAmounts):
    """Copy a :class:`LineAmounts` onto a line instance. Does not save.

    The low-level half of :func:`update_line`. Prefer that one: this writes the
    *derived* fields only, so on its own it will happily leave ``qty_input``
    describing one quantity and ``amount_paisa`` describing another.
    """
    line.qty_base = amounts.qty_base
    line.rate_paisa = amounts.rate_paisa
    line.amount_paisa = amounts.amount_paisa
    line.discount_paisa = amounts.discount_paisa
    line.tax_paisa = amounts.tax_paisa
    return line


def update_line(
    line,
    *,
    item,
    qty_input: int,
    unit_input: str,
    rate_input_paisa: int,
    discount_paisa: int = 0,
    tax_rate_bp: int | None = None,
):
    """Write everything a line holds, from what the operator typed. Does not save.

    **The way to fill in a line.** It sets what was typed *and* what was derived
    from it in one call, so the two can never describe different quantities.
    Setting the amounts alone would leave a line reading "10 cartons" and
    costing what six cartons cost — and since
    :meth:`~apps.purchasing.models.PurchaseLine.save` recomputes ``qty_base``
    from ``qty_input``, the quantity that actually posted would be the ten.
    """
    line.item = item
    line.qty_input = qty_input
    line.unit_input = unit_input
    return apply_line_amounts(
        line,
        compute_line(
            item,
            qty_input=qty_input,
            unit_input=unit_input,
            rate_input_paisa=rate_input_paisa,
            discount_paisa=discount_paisa,
            tax_rate_bp=tax_rate_bp,
        ),
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
class GLLine(NamedTuple):
    """One side of the posting, ready to render or to post.

    The preview on the entry screen and the rows that are actually written come
    from the **same** function. A preview that is computed separately is a
    preview that will eventually lie.
    """

    account: Account
    debit_paisa: int
    credit_paisa: int
    label: str

    def as_entry(self, party: PartyRef | None = None) -> dict:
        """The dict shape :func:`apps.accounting.services.post_entries` wants."""
        entry = {
            "account": self.account,
            "debit_paisa": self.debit_paisa,
            "credit_paisa": self.credit_paisa,
            "remarks": self.label,
        }
        if party is not None:
            entry["party"] = party
        return entry


def _accounts(*codes: str) -> dict[str, Account]:
    """Fetch the accounts a posting needs, in one query.

    Raises with the missing codes rather than a bare ``DoesNotExist``: an
    installation whose chart has been edited is exactly when this fails, and
    "account 4400 is missing" is the sentence that fixes it.
    """
    found = {account.code: account for account in Account.objects.filter(code__in=codes)}
    missing = [code for code in codes if code not in found]
    if missing:
        raise InvalidLine(
            f"The chart of accounts is missing {', '.join(missing)}. Run "
            f"`manage.py seed_chart_of_accounts` — it only creates what is absent."
        )
    return found


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

    account = _accounts(coa.INVENTORY, coa.TAX_PAYABLE, coa.DISCOUNT_RECEIVED, coa.ACCOUNTS_PAYABLE)

    gl = [GLLine(account[coa.INVENTORY], subtotal.paisa, 0, "Goods received")]
    if tax:
        gl.append(GLLine(account[coa.TAX_PAYABLE], tax.paisa, 0, "Input tax"))
    if discount:
        gl.append(GLLine(account[coa.DISCOUNT_RECEIVED], 0, discount.paisa, "Supplier discount"))
    gl.append(
        GLLine(account[coa.ACCOUNTS_PAYABLE], 0, total.paisa, f"Payable to {invoice.vendor.name}")
    )
    return [line for line in gl if line.debit_paisa or line.credit_paisa]


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

    account = _accounts(
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

    return [line for line in gl if line.debit_paisa or line.credit_paisa]


def assert_gl_balances(gl_lines, document) -> Money:
    """Debits == credits, to the paisa, before anything is written.

    :func:`~apps.accounting.services.post_entries` checks this too and is the
    real guarantee. This runs first so that a bug in *this* module's arithmetic
    is reported against the purchase document the operator is looking at, rather
    than as a generic unbalanced-voucher error a layer down.
    """
    debits = sum((Money(line.debit_paisa) for line in gl_lines), Money.zero())
    credits = sum((Money(line.credit_paisa) for line in gl_lines), Money.zero())
    if debits != credits:
        difference = debits - credits
        raise UnbalancedEntry(
            f"{type(document).__name__} {document.code} does not balance: debits "
            f"{debits.paisa} paisa vs credits {credits.paisa} paisa — a difference of "
            f"{difference.paisa} paisa. Nothing was written."
        )
    return debits


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
    _assert_inventory_matches_stock(gl_lines, movements, invoice)

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
    _assert_inventory_matches_stock(gl_lines, movements, document)

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


def _assert_inventory_matches_stock(gl_lines, movements, document) -> None:
    """The two ledgers must agree on what the goods were worth, to the paisa.

    This is the check the whole rounding design exists to pass. The Inventory
    line in the general ledger and the sum of the stock rows are two independent
    computations of the same fact; if they ever disagree, inventory value and
    the balance sheet have quietly parted company and every report after that is
    wrong by the difference.

    Both sides are signed the same way and no direction argument is needed:
    stock coming in is a positive movement and a debit, stock going out is a
    negative movement and a credit.
    """
    inventory = sum(
        (
            Money(line.debit_paisa) - Money(line.credit_paisa)
            for line in gl_lines
            if line.account.code == coa.INVENTORY
        ),
        Money.zero(),
    )
    stock_value = sum((Money(movement.value_paisa) for movement in movements), Money.zero())

    if inventory != stock_value:
        raise UnbalancedEntry(
            f"{type(document).__name__} {document.code}: the general ledger moves Inventory "
            f"by {inventory.paisa} paisa but the stock ledger moved {stock_value.paisa} "
            f"paisa. The two must agree exactly. Nothing was written."
        )


def _as_positive_int(value, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidLine(
            f"{label} must be a whole number as an int, got {type(value).__name__}: {value!r}"
        )
    if value <= 0:
        raise InvalidLine(f"{label} is {value}; a purchase line moves a positive quantity.")
    return value


def _as_non_negative_int(value, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidLine(
            f"{label} must be whole paisa as an int, got {type(value).__name__}: {value!r}. "
            f"Run operator input through apps.core.money.to_paisa first."
        )
    if value < 0:
        raise InvalidLine(f"{label} is {value}; it cannot be negative.")
    return value


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
