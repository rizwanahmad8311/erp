"""Sales invoices and sales returns: goods out to a shop, and goods back in.

The mirror of :mod:`apps.purchasing.models`, and deliberately the same shape —
two documents inheriting :class:`~apps.core.models.DocumentModel`, a shared
abstract header, a shared abstract line. Anyone who has read the purchasing app
already knows how this one is laid out.

Three things are genuinely different, and all three are on the way *out* rather
than the way in:

* **A sales line carries its cost.** ``cogs_paisa`` is captured at post time
  from the stock valuation and stored, because the moving average moves. See
  :class:`SalesLine`.
* **A sales invoice has a route and a seller.** Both default from the client and
  both can be overridden, because a booker sometimes covers someone else's beat.
* **A sales invoice can be refused.** The client's credit limit is checked
  before anything is written — see
  :func:`apps.sales.services.assert_within_credit_limit`.

The four money fields on each header are display conveniences and the source of
truth for nothing (CLAUDE.md §6). Every receivable, every sales figure and every
tax return is aggregated from :class:`~apps.accounting.models.LedgerEntry`. They
are recomputed from the lines by
:func:`~apps.sales.services.recalculate_totals` — never typed in, never adjusted
by hand — and each is an exact integer sum, so ``header == sum(lines)`` is
arithmetic rather than a tolerance.
"""

from __future__ import annotations

from django.db import models

from apps.core.fields import MoneyField, QuantityField
from apps.core.models import DocumentModel
from apps.masters.enums import Unit
from apps.masters.services import fmt_qty

from .exceptions import EmptyDocument, SalesError


class SalesDocument(DocumentModel):
    """What a sales invoice and a sales return have in common.

    Abstract. Two concrete tables rather than one with a type column, for the
    same reason purchasing has two: they post opposite entries, they are listed
    separately, they are numbered separately, and a shared table would make
    every query carry a type filter it could forget.
    """

    client = models.ForeignKey(
        "masters.Client",
        on_delete=models.PROTECT,
        related_name="%(class)ss",
        help_text="PROTECT: a client with documents cannot be deleted out from under them.",
    )
    warehouse = models.ForeignKey(
        "accounting.Warehouse",
        on_delete=models.PROTECT,
        related_name="%(class)ss",
        help_text="Where the goods leave from, or come back to. Valuation is per (item, warehouse).",
    )

    # Both default from the client and both are overridable. A route is a
    # property of the shop; who actually booked the order on the day is a
    # property of the document, and on the day someone is off sick those two
    # disagree. Recording the client's route on the invoice rather than reading
    # it back through the client also means a shop that moves beat next year
    # does not silently rewrite last year's route commission.
    route = models.ForeignKey(
        "masters.Route",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="%(class)ss",
        help_text="Defaults to the client's route. Override when the beat was covered.",
    )
    seller = models.ForeignKey(
        "masters.Seller",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="%(class)ss",
        help_text="Defaults to the client's usual booker. Override when someone covered.",
    )

    posting_date = models.DateField(
        db_index=True,
        help_text="The day this hits the books. Not the day it was typed in.",
    )

    subtotal_paisa = MoneyField(
        non_negative=True,
        help_text="Sum of the line amounts, before discount and tax. Exact, never rounded.",
    )
    discount_paisa = MoneyField(
        non_negative=True,
        help_text="Sum of the line discounts. Posted to Discount Allowed.",
    )
    tax_paisa = MoneyField(
        non_negative=True,
        help_text="Sum of the line tax. Output tax — it increases what is owed to the government.",
    )
    total_paisa = MoneyField(
        non_negative=True,
        help_text="subtotal - discount + tax. What the client owes, or is credited back.",
    )

    remarks = models.TextField(blank=True, default="")

    class Meta:
        abstract = True
        ordering = ["-posting_date", "-id"]

    def __str__(self) -> str:
        return f"{self.code} — {self.client.name} ({self.get_status_display()})"

    @property
    def line_count(self) -> int:
        return self.lines.count()

    @property
    def cogs_paisa(self) -> int:
        """What the goods on this document cost, summed from the lines.

        Zero until the document is posted: cost is captured from the stock
        valuation at post time and not before, because before then there is no
        answer — see :attr:`SalesLine.cogs_paisa`.
        """
        return sum(line.cogs_paisa for line in self.lines.all())

    def assert_has_lines(self) -> None:
        """Raise unless there is something to post."""
        if not self.lines.exists():
            raise EmptyDocument(
                f"{type(self).__name__} {self.code} has no lines. A document that moves "
                f"nothing should not reach either ledger."
            )

    def apply_client_defaults(self) -> None:
        """Fill the route and the seller in from the client, if they are blank.

        Only fills what is empty, so an override survives a re-save. Called when
        a draft is created and whenever the client changes on the entry screen.
        """
        if self.client_id is None:
            return
        if self.route_id is None:
            self.route_id = self.client.route_id
        if self.seller_id is None:
            self.seller_id = self.client.seller_id


class SalesInvoice(SalesDocument):
    """A shop's bill: goods out of a warehouse, money owed by the client.

    Posting writes, in one transaction (see
    :func:`~apps.sales.services.post_sales_invoice`):

    * a stock issue per line, valued at the moving weighted average, whose value
      is captured onto the line as ``cogs_paisa``;
    * Dr Accounts Receivable, Dr Discount Allowed, Cr Sales, Cr Tax Payable;
    * Dr Cost of Goods Sold, Cr Inventory.

    And it can be **refused**: a client already at their credit limit does not
    get more goods without someone holding ``sales.override_credit_limit``.
    """

    due_date = models.DateField(
        null=True,
        blank=True,
        db_index=True,
        help_text="When payment falls due. Defaults to posting_date + the client's credit days.",
    )

    class Meta(SalesDocument.Meta):
        verbose_name = "sales invoice"
        verbose_name_plural = "sales invoices"
        permissions = [
            (
                "override_credit_limit",
                "Can post a sales invoice that takes a client over their credit limit",
            ),
        ]

    # ------------------------------------------------------------------
    # Derived figures. Nothing here is stored.
    # ------------------------------------------------------------------
    @property
    def paid_paisa(self) -> int:
        """How much has been received against this invoice, in paisa.

        **Derived, never stored** (CLAUDE.md §6), through the same seam
        purchasing uses: ``apps.payments`` has no models yet, so this asks and
        finds nothing rather than being hardcoded to zero.
        """
        from apps.purchasing.services import payment_allocations

        return sum(allocation.amount_paisa for allocation in payment_allocations(self))

    @property
    def outstanding_paisa(self) -> int:
        return self.total_paisa - self.paid_paisa

    @property
    def is_paid(self) -> bool:
        return self.total_paisa > 0 and self.outstanding_paisa <= 0

    def default_due_date(self):
        """``posting_date`` plus the client's credit days. COD falls due same-day."""
        import datetime as dt

        if self.posting_date is None or self.client_id is None:
            return None
        return self.posting_date + dt.timedelta(days=self.client.credit_days)

    # ------------------------------------------------------------------
    # Lifecycle. The work is in services; these are the entry points.
    # ------------------------------------------------------------------
    def post(self, *, user=None, override_credit_limit: bool = False):
        from .services import post_sales_invoice

        return post_sales_invoice(self, user=user, override_credit_limit=override_credit_limit)

    def cancel(self, *, user=None, reason: str = ""):
        from .services import cancel_sales_invoice

        return cancel_sales_invoice(self, user=user, reason=reason)

    def amend(self, *, user=None):
        from .services import amend_sales_invoice

        return amend_sales_invoice(self, user=user)


class SalesReturn(SalesDocument):
    """A credit note: goods back from a shop, money owed reduced.

    The invoice mirrored, with one thing the purchase return could not have: the
    goods come back in at **what they cost when they left**, taken from the
    original invoice line when this note names one. Nothing has to be estimated
    and no gain or loss line is needed — inventory is restored to exactly the
    value it gave up.

    ``against_invoice`` is optional because a shop returns goods months later
    with no paperwork, and refusing the credit note is not an option. Without it
    the goods come back at the current moving average, which is the best answer
    available.
    """

    against_invoice = models.ForeignKey(
        SalesInvoice,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="returns",
        help_text=(
            "The invoice these goods went out on, if it is known. Sets what they come "
            "back into stock at, and lets the quantities be checked."
        ),
    )

    class Meta(SalesDocument.Meta):
        verbose_name = "sales return"
        verbose_name_plural = "sales returns"

    def post(self, *, user=None):
        from .services import post_sales_return

        return post_sales_return(self, user=user)

    def cancel(self, *, user=None, reason: str = ""):
        from .services import cancel_sales_return

        return cancel_sales_return(self, user=user, reason=reason)

    def amend(self, *, user=None):
        from .services import amend_sales_return

        return amend_sales_return(self, user=user)


# ===========================================================================
# Lines
# ===========================================================================
class SalesLine(models.Model):
    """One item on a sales document, in the unit it was typed in.

    Identical to :class:`apps.purchasing.models.PurchaseLine` in every field it
    shares — same three quantities, same anchored ``amount_paisa``, same derived
    ``rate_paisa`` — because it is the same arithmetic, from
    :mod:`apps.masters.pricing`. Read that module for why the amount is the
    anchor and the per-base-unit rate is the derivation.

    What it adds is ``cogs_paisa``, and the word that matters is *captured*.
    """

    item = models.ForeignKey(
        "masters.Item",
        on_delete=models.PROTECT,
        related_name="%(class)ss",
        help_text="PROTECT: an item on a posted document cannot be deleted.",
    )

    qty_input = QuantityField(
        help_text="Quantity exactly as typed, in unit_input. Kept so the line can be re-shown.",
    )
    unit_input = models.CharField(
        max_length=8,
        choices=Unit.choices,
        default=Unit.PIECE,
        help_text="The unit qty_input is counted in. PIECE or CARTON.",
    )
    qty_base = QuantityField(
        help_text="qty_input converted to whole base units. Computed on save via masters.to_base.",
    )

    rate_paisa = MoneyField(
        non_negative=True,
        help_text=(
            "Selling price of ONE BASE UNIT, in paisa. Derived from amount_paisa and "
            "rounded once — the amount is the figure that was agreed, not this."
        ),
    )
    discount_paisa = MoneyField(
        non_negative=True,
        help_text="Discount on this line, in paisa. Posted to Discount Allowed.",
    )
    tax_paisa = MoneyField(
        non_negative=True,
        help_text="Sales tax on this line, in paisa. Output tax on a sale.",
    )
    amount_paisa = MoneyField(
        non_negative=True,
        help_text=(
            "qty_input x the rate that was typed, in paisa. Exact and never rounded. "
            "This is what the client was billed."
        ),
    )

    cogs_paisa = MoneyField(
        non_negative=True,
        help_text=(
            "What these goods cost us, in paisa. CAPTURED AT POST TIME from the stock "
            "valuation and frozen there. Zero on a draft — before posting there is no answer."
        ),
    )

    class Meta:
        abstract = True
        ordering = ["id"]
        constraints = [
            models.CheckConstraint(
                name="%(app_label)s_%(class)s_qty_input_positive",
                condition=models.Q(qty_input__gt=0),
                violation_error_message="A line moves a positive quantity.",
            ),
            models.CheckConstraint(
                name="%(app_label)s_%(class)s_qty_base_positive",
                condition=models.Q(qty_base__gt=0),
                violation_error_message="A line moves a positive number of base units.",
            ),
            models.CheckConstraint(
                name="%(app_label)s_%(class)s_discount_within_amount",
                condition=models.Q(discount_paisa__lte=models.F("amount_paisa")),
                violation_error_message="A line discount cannot exceed the line amount.",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.item.code} x {self.qty_input} {self.unit_input}"

    def save(self, *args, **kwargs):
        """Derive ``qty_base``, then refuse the write if the parent is frozen.

        Deriving a stored column in ``save()`` is not the thing CLAUDE.md §4
        forbids — nothing is posted here, it is a pure function of two fields on
        the same row. The immutability check is here because ``DocumentModel``
        can only guard its own row, and a POSTED invoice whose lines could still
        be edited is a document whose ledger entries no longer describe it.
        """
        from apps.masters.services import to_base

        self.qty_base = to_base(self.item, self.qty_input, self.unit_input)
        self._assert_parent_editable("modified")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        self._assert_parent_editable("deleted")
        return super().delete(*args, **kwargs)

    # ------------------------------------------------------------------
    # Derived figures. Nothing here is stored.
    # ------------------------------------------------------------------
    @property
    def net_paisa(self) -> int:
        """Line amount after its discount, before tax. Exact integer subtraction."""
        return self.amount_paisa - self.discount_paisa

    @property
    def total_paisa(self) -> int:
        """What this line contributes to the document total."""
        return self.net_paisa + self.tax_paisa

    @property
    def margin_paisa(self) -> int:
        """Net revenue less cost. Meaningless until the document is posted."""
        return self.net_paisa - self.cogs_paisa

    @property
    def qty_display(self) -> str:
        """``"3 ctn + 5 pcs"`` — through masters, which owns that arithmetic."""
        return fmt_qty(self.item, self.qty_base)

    @property
    def rate_is_exact(self) -> bool:
        """True when ``qty_base * rate_paisa`` lands back on ``amount_paisa``.

        Not a validity check — a False here is normal and correct. It marks a
        line whose per-unit rate is a rounded figure, so nobody reads the stock
        card and thinks the bill is wrong.
        """
        return self.qty_base * self.rate_paisa == self.amount_paisa

    def _assert_parent_editable(self, verb: str) -> None:
        document = self.document
        if document is not None and not document.is_editable:
            raise SalesError(
                f"{type(document).__name__} {document.code} is {document.status}; its lines "
                f"cannot be {verb}. Cancel it and post an amendment instead."
            )


class SalesInvoiceLine(SalesLine):
    document = models.ForeignKey(
        SalesInvoice,
        on_delete=models.CASCADE,
        related_name="lines",
        help_text="CASCADE: a line has no meaning without its invoice, and a DRAFT may be deleted.",
    )

    class Meta(SalesLine.Meta):
        verbose_name = "sales invoice line"
        verbose_name_plural = "sales invoice lines"


class SalesReturnLine(SalesLine):
    document = models.ForeignKey(
        SalesReturn,
        on_delete=models.CASCADE,
        related_name="lines",
        help_text="CASCADE: a line has no meaning without its return, and a DRAFT may be deleted.",
    )

    class Meta(SalesLine.Meta):
        verbose_name = "sales return line"
        verbose_name_plural = "sales return lines"
