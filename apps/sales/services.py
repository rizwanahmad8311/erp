"""Sales invoice and sales return: the two postings, and the credit limit.

Everything that writes a ledger or stock row for a sales document is in this
module, wrapped in ``transaction.atomic()`` (CLAUDE.md §4). Views and admin
actions call these functions; they never build a ledger row themselves.

The line arithmetic is :mod:`apps.masters.pricing`, shared with purchasing and
re-exported here — the amount a client was billed is exact, the per-base-unit
rate is derived from it. What sales adds is on the way *out*:

**Cost is captured, not read.** A sales line records ``cogs_paisa`` at post
time, taken from the value the stock ledger actually released. It is stored
rather than derived because the moving weighted average *moves*: the next goods
receipt changes what today's stock is worth, and an invoice whose margin
silently rewrote itself every time someone bought more is an invoice nobody can
reconcile. See :func:`post_sales_invoice`.

**A sale can be refused.** :func:`assert_within_credit_limit` runs before
anything is written, and a client already at their limit does not get more goods
without someone holding ``sales.override_credit_limit``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

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
    party_balance,
    post_entries,
    post_stock,
    reverse_entries,
    reverse_stock,
    stock_balance,
    valuation_rate,
)
from apps.accounting.valuation import Position
from apps.core.enums import DocumentStatus
from apps.core.money import Money
from apps.core.services import get_next_code

# The line arithmetic is masters', not sales': every argument to it is an item
# or a number, and purchasing asks the identical question in the other
# direction. Re-exported so a sales caller has one import.
from apps.masters.pricing import (
    LineAmounts,
    apply_line_amounts,
    compute_line,
    entry_rate_paisa,
    update_line,
)

from .enums import SALES_INVOICE_PREFIX, SALES_RETURN_PREFIX
from .exceptions import CreditLimitExceeded, ReturnExceedsInvoice
from .models import (
    SalesInvoice,
    SalesInvoiceLine,
    SalesReturn,
    SalesReturnLine,
)

#: The permission that lets an invoice past the credit limit.
OVERRIDE_CREDIT_LIMIT = "sales.override_credit_limit"


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
# Credit limit
# ===========================================================================
@dataclass(frozen=True, slots=True)
class CreditStatus:
    """Where a client stands, and where this document would put them.

    Every figure comes from the ledger (CLAUDE.md §6) except the limit, which is
    a master. Nothing here is cached anywhere.
    """

    client: object
    limit_paisa: int
    balance_paisa: int
    document_paisa: int

    @property
    def would_owe_paisa(self) -> int:
        return self.balance_paisa + self.document_paisa

    @property
    def overage_paisa(self) -> int:
        """How far past the limit this document lands. Negative means headroom."""
        return self.would_owe_paisa - self.limit_paisa

    @property
    def is_within_limit(self) -> bool:
        return self.would_owe_paisa <= self.limit_paisa

    @property
    def headroom_paisa(self) -> int:
        """What is left to sell before the limit bites. Never negative."""
        return max(self.limit_paisa - self.balance_paisa, 0)


def credit_status(document) -> CreditStatus:
    """What the client owes now, and what this document would add.

    The balance is aggregated from the ledger through
    :func:`~apps.accounting.services.party_balance`, in the client's natural
    sign — positive means *they owe us*. It counts every posted invoice, credit
    note and receipt they have ever had, which is the only figure a credit
    decision can honestly be made on.

    A draft's total is recomputed from its lines here, in memory and without
    saving, rather than trusting the header. A draft that has just had a line
    added has a stale header until something recalculates it, and a credit check
    against a stale header is a credit check that passes an invoice it should
    have refused.
    """
    if document.is_editable:
        recalculate_totals(document, save=False)

    balance = party_balance(PartyType.CLIENT, document.client_id)
    return CreditStatus(
        client=document.client,
        limit_paisa=document.client.credit_limit_paisa,
        balance_paisa=balance.paisa,
        document_paisa=document.total_paisa,
    )


def may_override_credit_limit(user) -> bool:
    """Whether this user holds ``sales.override_credit_limit``.

    ``None`` counts as allowed: a caller with no user is a management command, a
    data migration or a test harness, all of which are already trusted code. A
    request always has a user, so nothing reachable from a browser gets in this
    way.
    """
    if user is None:
        return True
    return user.has_perm(OVERRIDE_CREDIT_LIMIT)


def assert_within_credit_limit(invoice, *, user=None, override: bool = False) -> CreditStatus:
    """Raise unless this invoice may be posted against the client's limit.

    The rule is the plain one: what they already owe, plus what this invoice
    comes to, must not exceed the limit. ``credit_limit_paisa = 0`` therefore
    means *no credit at all* and blocks any invoice — which is what the field's
    own help text says it means, and what a cash-only shop should do.

    ``override=True`` skips the check, but only for somebody who actually holds
    the permission. The flag arrives from a form, so it is not trusted on its
    own; a request from a user without the permission is refused with the same
    message as if they had not asked.

    Returns the status, so a caller that wants to log or display the figures
    does not have to recompute them.
    """
    status = credit_status(invoice)
    if status.is_within_limit:
        return status

    if override and may_override_credit_limit(user):
        return status

    raise CreditLimitExceeded(
        client=status.client,
        limit_paisa=status.limit_paisa,
        balance_paisa=status.balance_paisa,
        total_paisa=status.document_paisa,
    )


# ===========================================================================
# The general ledger side
# ===========================================================================
def build_invoice_gl(
    invoice,
    *,
    cogs_paisa: int,
    subtotal_paisa=None,
    discount_paisa=None,
    tax_paisa=None,
):
    """The general ledger a sales invoice posts.

        Dr Accounts Receivable   what the client owes, tagged with the client
        Dr Discount Allowed      the discount given, taken as an expense
        Cr Sales                 the goods, at list price
        Cr Tax Payable           output tax, which we owe the government
        Dr Cost of Goods Sold    what those goods cost us
        Cr Inventory             the same figure, leaving stock

    The mirror of :func:`apps.purchasing.services.build_invoice_gl`: revenue is
    credited **gross** and the discount is a separate debit, exactly as a
    purchase debits Inventory gross and credits Discount Received. Both balance;
    this way round, the discount given is visible as its own expense rather than
    buried in a lower sales figure.

    The cost pair is what makes a sale a sale rather than a transfer of cash.
    ``cogs_paisa`` is what the **stock ledger actually released**, not
    ``qty x some rate`` — see :func:`post_sales_invoice`.

    Balances exactly, by construction and with no rounding anywhere:
    ``total + discount + cogs == subtotal + tax + cogs``, since ``total`` is
    defined as ``subtotal - discount + tax``.

    The amounts default to the header's, and are accepted as arguments so the
    entry screen can preview a document before it is posted.
    """
    subtotal = Money(invoice.subtotal_paisa if subtotal_paisa is None else subtotal_paisa)
    discount = Money(invoice.discount_paisa if discount_paisa is None else discount_paisa)
    tax = Money(invoice.tax_paisa if tax_paisa is None else tax_paisa)
    total = subtotal - discount + tax
    cogs = Money(cogs_paisa)

    account = accounts_by_code(
        coa.ACCOUNTS_RECEIVABLE,
        coa.DISCOUNT_ALLOWED,
        coa.SALES,
        coa.TAX_PAYABLE,
        coa.COST_OF_GOODS_SOLD,
        coa.INVENTORY,
    )

    gl = [
        GLLine(
            account[coa.ACCOUNTS_RECEIVABLE],
            total.paisa,
            0,
            f"Receivable from {invoice.client.name}",
        ),
        GLLine(account[coa.DISCOUNT_ALLOWED], discount.paisa, 0, "Discount allowed"),
        GLLine(account[coa.SALES], 0, subtotal.paisa, "Sales"),
        GLLine(account[coa.TAX_PAYABLE], 0, tax.paisa, "Output tax"),
        GLLine(account[coa.COST_OF_GOODS_SOLD], cogs.paisa, 0, "Cost of goods sold"),
        GLLine(account[coa.INVENTORY], 0, cogs.paisa, "Goods issued, at cost"),
    ]
    return drop_zero_lines(gl)


def build_return_gl(
    document,
    *,
    cogs_paisa: int,
    subtotal_paisa=None,
    discount_paisa=None,
    tax_paisa=None,
):
    """The general ledger a sales return posts — the invoice's, mirrored.

        Cr Accounts Receivable   what the client no longer owes
        Dr Sales Returns         the goods coming back, at what they were sold for
        Dr Tax Payable           the output tax, reversed
        Cr Discount Allowed      the discount, taken back
        Dr Inventory             the goods, at what they cost when they left
        Cr Cost of Goods Sold    the same figure, unwinding the cost

    Revenue comes back through **4200 Sales Returns** rather than as a debit to
    4100 Sales. That account is a contra-income account and exists for exactly
    this: it nets against Sales when the Income group is totalled, and it leaves
    "what did we sell" and "what came back" as two figures somebody can look at
    separately.

    Unlike a purchase return, no gain or loss line is ever needed here. A
    purchase return has to release stock at the moving average and take whatever
    difference falls out; a sales return **puts stock back at the cost it left
    at**, which the original invoice recorded (CLAUDE.md's whole reason for
    capturing it). Inventory is restored to exactly the value it gave up, so the
    two cost rows cancel and the ledger balances with nothing left over.
    """
    subtotal = Money(document.subtotal_paisa if subtotal_paisa is None else subtotal_paisa)
    discount = Money(document.discount_paisa if discount_paisa is None else discount_paisa)
    tax = Money(document.tax_paisa if tax_paisa is None else tax_paisa)
    total = subtotal - discount + tax
    cogs = Money(cogs_paisa)

    account = accounts_by_code(
        coa.ACCOUNTS_RECEIVABLE,
        coa.DISCOUNT_ALLOWED,
        coa.SALES_RETURNS,
        coa.TAX_PAYABLE,
        coa.COST_OF_GOODS_SOLD,
        coa.INVENTORY,
    )

    gl = [
        GLLine(account[coa.SALES_RETURNS], subtotal.paisa, 0, "Sales returned"),
        GLLine(account[coa.TAX_PAYABLE], tax.paisa, 0, "Output tax reversed"),
        GLLine(account[coa.DISCOUNT_ALLOWED], 0, discount.paisa, "Discount taken back"),
        GLLine(
            account[coa.ACCOUNTS_RECEIVABLE],
            0,
            total.paisa,
            f"Credited to {document.client.name}",
        ),
        GLLine(account[coa.INVENTORY], cogs.paisa, 0, "Goods returned to stock, at cost"),
        GLLine(account[coa.COST_OF_GOODS_SOLD], 0, cogs.paisa, "Cost of goods sold unwound"),
    ]
    return drop_zero_lines(gl)


# ===========================================================================
# Sales invoice
# ===========================================================================
@transaction.atomic
def post_sales_invoice(
    invoice: SalesInvoice, *, user=None, override_credit_limit: bool = False
) -> SalesInvoice:
    """Issue the goods and record what is owed. All of it, or none of it.

    Order matters and is not arbitrary:

    1. **Totals**, so the credit check has a figure to check.
    2. **The credit limit**, before a single row is written. A refusal here must
       leave the invoice a clean DRAFT the operator can still edit.
    3. **Stock out**, valued by the stock ledger at the moving weighted average.
       The value it releases is the cost of the sale, and nothing else can tell
       you what that is.
    4. **Cost onto the lines** — captured, then frozen. See below.
    5. **The general ledger**, built from that same cost so the two ledgers
       agree to the paisa.

    Step 4 is the one worth pausing on. ``cogs_paisa`` is written from the
    movement the stock ledger just made, not recomputed later from
    ``valuation_rate``. The average moves every time goods are received, so a
    margin derived at read time would be a different number next week, on an
    immutable document, with no record of what it used to be.
    """
    invoice.assert_transition(DocumentStatus.POSTED)
    invoice.assert_has_lines()

    recalculate_totals(invoice)
    assert_within_credit_limit(invoice, user=user, override=override_credit_limit)

    lines = list(invoice.lines.select_related("item"))
    movements = post_stock(
        invoice,
        [
            {
                "item": line.item,
                "warehouse": invoice.warehouse,
                # Negative: stock going out. No rate and no value — an issue is
                # valued by the stock ledger at the average it holds, and a
                # selling price must never reach it, or cost of goods sold
                # becomes the sale and every margin is zero.
                "qty_base": -line.qty_base,
            }
            for line in lines
        ],
        invoice.posting_date,
        user=user,
    )

    cogs_total = _capture_cogs(lines, movements, SalesInvoiceLine)

    gl_lines = build_invoice_gl(invoice, cogs_paisa=cogs_total)
    assert_gl_balances(gl_lines, invoice)
    assert_inventory_matches_stock(gl_lines, movements, invoice)

    party = PartyRef(PartyType.CLIENT, invoice.client_id)
    post_entries(
        invoice,
        [
            gl.as_entry(party if gl.account.code == coa.ACCOUNTS_RECEIVABLE else None)
            for gl in gl_lines
        ],
        invoice.posting_date,
        user=user,
    )

    invoice.mark_posted(user=user)
    invoice.save()
    return invoice


@transaction.atomic
def cancel_sales_invoice(invoice: SalesInvoice, *, user=None, reason: str = "") -> SalesInvoice:
    """Reverse everything this invoice wrote. Refuses if it has been paid.

    Cancelling writes mirror rows into both ledgers; it never touches the
    originals (CLAUDE.md §3). The captured ``cogs_paisa`` is left on the lines
    exactly as it was — the reversal puts the stock back at the rate it went out
    at, so the cost that was recorded is the cost that was unwound, and rubbing
    it out would destroy the only record of what the sale actually cost.
    """
    from apps.purchasing.services import assert_not_paid

    invoice.assert_transition(DocumentStatus.CANCELLED)
    assert_not_paid(invoice)

    reverse_stock(invoice, user=user)
    reverse_entries(invoice, user=user)

    invoice.mark_cancelled(user=user, reason=reason)
    invoice.save()
    return invoice


@transaction.atomic
def amend_sales_invoice(invoice: SalesInvoice, *, user=None) -> SalesInvoice:
    """Clone a CANCELLED invoice into a new DRAFT, lines and all.

    The cost is deliberately **not** carried across. An amendment is a new sale
    that will issue stock of its own, at whatever the average is on the day it
    posts; copying the old figure over would put a cost on a draft that has
    released nothing.
    """
    amendment = invoice.build_amendment(user=user)
    _copy_lines(invoice, amendment, SalesInvoiceLine)
    recalculate_totals(amendment)
    return amendment


# ===========================================================================
# Sales return
# ===========================================================================
@transaction.atomic
def post_sales_return(document: SalesReturn, *, user=None) -> SalesReturn:
    """Take the goods back and credit the client. All of it, or none of it.

    The cost the goods come back in at is decided **before** the stock posting,
    because unlike an issue, a receipt has to be told what it is worth. See
    :func:`return_cost_paisa`.
    """
    document.assert_transition(DocumentStatus.POSTED)
    document.assert_has_lines()

    recalculate_totals(document)
    lines = list(document.lines.select_related("item"))

    if document.against_invoice_id is not None:
        assert_return_within_invoice(document, lines)

    costs = [return_cost_paisa(document, line) for line in lines]

    movements = post_stock(
        document,
        [
            {
                "item": line.item,
                "warehouse": document.warehouse,
                # Positive: stock coming back. Valued at what it cost when it
                # left, which the original invoice captured — so inventory is
                # restored to exactly the value it gave up.
                "qty_base": line.qty_base,
                "value_paisa": cost,
            }
            for line, cost in zip(lines, costs, strict=True)
        ],
        document.posting_date,
        user=user,
    )

    cogs_total = _capture_cogs(lines, movements, SalesReturnLine)

    gl_lines = build_return_gl(document, cogs_paisa=cogs_total)
    assert_gl_balances(gl_lines, document)
    assert_inventory_matches_stock(gl_lines, movements, document)

    party = PartyRef(PartyType.CLIENT, document.client_id)
    post_entries(
        document,
        [
            gl.as_entry(party if gl.account.code == coa.ACCOUNTS_RECEIVABLE else None)
            for gl in gl_lines
        ],
        document.posting_date,
        user=user,
    )

    document.mark_posted(user=user)
    document.save()
    return document


@transaction.atomic
def cancel_sales_return(document: SalesReturn, *, user=None, reason: str = "") -> SalesReturn:
    """Reverse everything this credit note wrote."""
    from apps.purchasing.services import assert_not_paid

    document.assert_transition(DocumentStatus.CANCELLED)
    assert_not_paid(document)

    reverse_stock(document, user=user)
    reverse_entries(document, user=user)

    document.mark_cancelled(user=user, reason=reason)
    document.save()
    return document


@transaction.atomic
def amend_sales_return(document: SalesReturn, *, user=None) -> SalesReturn:
    """Clone a CANCELLED credit note into a new DRAFT, lines and all."""
    amendment = document.build_amendment(user=user)
    _copy_lines(document, amendment, SalesReturnLine)
    recalculate_totals(amendment)
    return amendment


def return_cost_paisa(document, line) -> int:
    """What one return line's goods come back into stock at, in paisa.

    Two cases, and the first is the one worth having:

    * **The credit note names the invoice.** The goods come back at what that
      invoice recorded them costing. A partial return takes its share through
      :meth:`~apps.core.money.Money.allocate`, whose parts sum back to the
      original exactly, so returning a line in three pieces puts back precisely
      what one piece took out — no paisa lost, no paisa invented.
    * **It does not.** A shop returns goods months later with no paperwork, and
      refusing the credit note is not an option. They come back at the current
      moving average, which is the best answer available and is stated as such
      on the screen.

    Never at the *selling* price. Bringing stock back in at what it sold for
    would book the margin into inventory and quietly inflate the balance sheet
    every time anybody returned anything.
    """
    original = _original_line(document, line)
    if original is None:
        rate = valuation_rate(line.item, document.warehouse, document.posting_date)
        return (Money(rate) * line.qty_base).paisa

    if line.qty_base >= original.qty_base:
        return original.cogs_paisa

    returning, _staying = Money(original.cogs_paisa).allocate(
        [line.qty_base, original.qty_base - line.qty_base]
    )
    return returning.paisa


def assert_return_within_invoice(document, lines) -> None:
    """Raise if this credit note sends back more than the invoice sold.

    Counts what other **posted** credit notes against the same invoice have
    already taken back, so three partial returns cannot quietly add up to more
    than went out. Drafts are not counted: they have written nothing, and two
    operators each holding a draft should both be told when the second one
    posts, not when the first one is typed.
    """
    invoice = document.against_invoice
    sold = {}
    for original in invoice.lines.all():
        sold[original.item_id] = sold.get(original.item_id, 0) + original.qty_base

    already = {}
    previous = SalesReturnLine.objects.filter(
        document__against_invoice=invoice,
        document__status=DocumentStatus.POSTED,
    ).exclude(document_id=document.pk)
    for earlier in previous:
        already[earlier.item_id] = already.get(earlier.item_id, 0) + earlier.qty_base

    for line in lines:
        went_out = sold.get(line.item_id, 0)
        came_back = already.get(line.item_id, 0)
        if line.qty_base + came_back > went_out:
            raise ReturnExceedsInvoice(
                f"{invoice.code} sold {went_out} base unit(s) of {line.item.code} and "
                f"{came_back} have already come back. This note returns {line.qty_base} "
                f"more, which is {line.qty_base + came_back - went_out} more than left."
            )


def preview_cogs_paisa(document) -> int:
    """What a document's cost *would* be, for the entry screen's preview.

    An estimate, and labelled as one wherever it is shown. The figure that gets
    posted is computed by :func:`~apps.accounting.services.post_stock` under the
    write lock; this walks the same :class:`~apps.accounting.valuation.Position`
    arithmetic outside it, so a document posted a moment later will normally
    agree exactly, and will differ if someone else moved the same stock in
    between. Nothing is written here.
    """
    if isinstance(document, SalesReturn):
        return sum(
            return_cost_paisa(document, line) for line in document.lines.select_related("item")
        )

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


def available_stock(document, item) -> int:
    """Base units of ``item`` on hand in this document's warehouse.

    Shown against every line on the entry screen. Aggregated from the stock
    ledger on every render — there is no ``stock_on_hand`` column anywhere and
    there must not be (CLAUDE.md §6).
    """
    return stock_balance(item, document.warehouse, document.posting_date).qty_base


# ===========================================================================
# Draft creation
# ===========================================================================
@transaction.atomic
def create_sales_invoice(*, client, warehouse, posting_date: date, **fields) -> SalesInvoice:
    """A new DRAFT invoice with a freshly allocated code.

    The route, the seller and the due date all default from the client and all
    three can be overridden — pass them explicitly and nothing here touches
    them. The code comes from :func:`apps.core.services.get_next_code` inside
    this same transaction, so a failed save does not burn a number.
    """
    invoice = SalesInvoice(
        code=get_next_code(SALES_INVOICE_PREFIX, fiscal_year_of(posting_date)),
        client=client,
        warehouse=warehouse,
        posting_date=posting_date,
        **fields,
    )
    invoice.apply_client_defaults()
    if invoice.due_date is None:
        invoice.due_date = invoice.default_due_date()
    invoice.save()
    return invoice


@transaction.atomic
def create_sales_return(*, client, warehouse, posting_date: date, **fields) -> SalesReturn:
    """A new DRAFT credit note with a freshly allocated code."""
    document = SalesReturn(
        code=get_next_code(SALES_RETURN_PREFIX, fiscal_year_of(posting_date)),
        client=client,
        warehouse=warehouse,
        posting_date=posting_date,
        **fields,
    )
    document.apply_client_defaults()
    document.save()
    return document


def fiscal_year_of(posting_date: date) -> int:
    """Which numbering year a document belongs to.

    The calendar year of the posting date, matching
    :func:`apps.purchasing.services.fiscal_year_of`. When this installation gets
    a real fiscal-year policy, both change together.
    """
    return posting_date.year


# ===========================================================================
# Internals
# ===========================================================================
def _capture_cogs(lines, movements, line_model) -> int:
    """Write the stock ledger's own valuation onto the lines. Returns the total.

    Paired by **position**, not by item: ``post_stock`` writes one row per line
    in the order it was given them, and two lines on one invoice may name the
    same item. ``strict=True`` turns a future change in that contract into an
    exception here rather than into a silently mismatched cost.

    ``bulk_update`` rather than a save loop: these are document lines, not
    ledger rows, the document is still a DRAFT at this point, and the only
    column being written is one that nothing has read yet.
    """
    total = Money.zero()
    for line, movement in zip(lines, movements, strict=True):
        line.cogs_paisa = abs(movement.value_paisa)
        total += Money(line.cogs_paisa)

    if lines:
        line_model.objects.bulk_update(lines, ["cogs_paisa"])
    return total.paisa


def _original_line(document, line):
    """The invoice line these goods went out on, if this note names an invoice."""
    if document.against_invoice_id is None:
        return None
    return document.against_invoice.lines.filter(item_id=line.item_id).first()


def _copy_lines(source, target, line_model) -> None:
    """Copy every line from one document onto another, amounts and all.

    The amounts are carried across rather than recomputed: an amendment starts
    as an exact copy of what was cancelled, and the operator changes the one
    thing that was wrong. ``cogs_paisa`` is deliberately **not** copied — the
    amendment has released no stock, so it has no cost yet.
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
                cogs_paisa=0,
            )
            for line in source.lines.select_related("item")
        ]
    )


__all__ = [
    "OVERRIDE_CREDIT_LIMIT",
    "CreditStatus",
    "LineAmounts",
    "amend_sales_invoice",
    "amend_sales_return",
    "apply_line_amounts",
    "assert_return_within_invoice",
    "assert_within_credit_limit",
    "available_stock",
    "build_invoice_gl",
    "build_return_gl",
    "cancel_sales_invoice",
    "cancel_sales_return",
    "compute_line",
    "create_sales_invoice",
    "create_sales_return",
    "credit_status",
    "entry_rate_paisa",
    "fiscal_year_of",
    "may_override_credit_limit",
    "post_sales_invoice",
    "post_sales_return",
    "preview_cogs_paisa",
    "recalculate_totals",
    "return_cost_paisa",
    "update_line",
]
