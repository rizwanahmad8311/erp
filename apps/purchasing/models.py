"""Purchase invoices and purchase returns: goods in from a supplier, and goods
back out to them.

Two documents, mirror images of each other, both inheriting
:class:`~apps.core.models.DocumentModel` and therefore both DRAFT -> POSTED ->
CANCELLED and nothing else (CLAUDE.md §5). Neither writes a ledger row in
``save()`` — posting lives in :mod:`apps.purchasing.services`, wrapped in
``transaction.atomic()`` (CLAUDE.md §4).

About the four money fields on each header
------------------------------------------
``subtotal_paisa``, ``discount_paisa``, ``tax_paisa`` and ``total_paisa`` are
**display conveniences on the document that owns them, and the source of truth
for nothing** (CLAUDE.md §6). No report reads them; every payables figure,
every purchase total and every tax return is aggregated from
:class:`~apps.accounting.models.LedgerEntry`. They exist so the entry screen and
the printed bill can show a total without replaying the lines, and they are
recomputed from the lines by
:func:`~apps.purchasing.services.recalculate_totals` — never typed in, never
adjusted by hand.

Each of the four is an **exact integer sum of the lines**. Nothing is rounded at
header level, so ``header == sum(lines)`` is not an approximation that holds most
of the time; it is arithmetic.

``paid_paisa`` is deliberately **not** a field. See :attr:`PurchaseInvoice.paid_paisa`.
"""

from __future__ import annotations

from django.db import models
from django.urls import reverse

from apps.core.enums import DocumentStatus
from apps.core.fields import MoneyField, QuantityField
from apps.core.lifecycle import Dependent, payment_dependents
from apps.core.models import DocumentModel
from apps.masters.enums import Unit
from apps.masters.services import fmt_qty

from .exceptions import PurchasingError


class PurchaseDocument(DocumentModel):
    """What a purchase invoice and a purchase return have in common.

    Abstract. Two concrete tables rather than one with a type column, for the
    same reason :class:`~apps.masters.models.Client` and
    ``Vendor`` are separate: they post opposite entries, they are listed
    separately, they are numbered separately, and a shared table would make
    every query carry a type filter it could forget.
    """

    vendor = models.ForeignKey(
        "masters.Vendor",
        on_delete=models.PROTECT,
        related_name="%(class)ss",
        help_text="PROTECT: a vendor with documents cannot be deleted out from under them.",
    )
    warehouse = models.ForeignKey(
        "accounting.Warehouse",
        on_delete=models.PROTECT,
        related_name="%(class)ss",
        help_text="Where the goods land, or leave from. Valuation is per (item, warehouse).",
    )

    posting_date = models.DateField(
        db_index=True,
        help_text="The day this hits the books. Not the day it was typed in.",
    )
    vendor_bill_no = models.CharField(
        max_length=64,
        blank=True,
        default="",
        db_index=True,
        help_text="The supplier's own document number, as printed on their bill.",
    )
    vendor_bill_date = models.DateField(
        null=True,
        blank=True,
        help_text="The date on the supplier's bill, which is often not our posting date.",
    )

    # Display conveniences, recomputed from the lines. See the module docstring:
    # no report reads these, and nothing here is the source of truth.
    subtotal_paisa = MoneyField(
        non_negative=True,
        help_text="Sum of the line amounts, before discount and tax. Exact, never rounded.",
    )
    discount_paisa = MoneyField(
        non_negative=True,
        help_text="Sum of the line discounts. Posted to Discount Received.",
    )
    tax_paisa = MoneyField(
        non_negative=True,
        help_text="Sum of the line tax. Input tax — it reduces what is owed to the government.",
    )
    total_paisa = MoneyField(
        non_negative=True,
        help_text="subtotal - discount + tax. What the supplier is owed, or credits back.",
    )

    remarks = models.TextField(blank=True, default="")

    class Meta:
        abstract = True
        ordering = ["-posting_date", "-id"]

    #: The URL slug the purchase screens are parameterised by. Set per subclass;
    #: :meth:`get_absolute_url` is the only thing that reads it, so the shared
    #: timeline and cancel templates can link to any document without knowing
    #: which type they are holding.
    URL_SLUG = ""

    def __str__(self) -> str:
        return f"{self.code} — {self.vendor.name} ({self.get_status_display()})"

    def get_absolute_url(self) -> str:
        return reverse("purchasing:detail", kwargs={"slug": self.URL_SLUG, "pk": self.pk})

    # ------------------------------------------------------------------
    # Reading the lines
    # ------------------------------------------------------------------
    @property
    def line_count(self) -> int:
        return self.lines.count()

    # ------------------------------------------------------------------
    # What blocks a cancellation
    # ------------------------------------------------------------------
    def dependents(self) -> list[Dependent]:
        """Money allocated against this document, and nothing else.

        A purchase document has no returns raised *against* it the way a sales
        invoice does — a purchase return is its own document with its own stock
        movement and no link back — so the payments are the whole of it.
        """
        return payment_dependents(self)

    def assert_has_lines(self) -> None:
        """Raise unless there is something to post.

        Called at the top of a posting service. A document with no lines moves
        no goods and no money, and posting one puts a code and a supplier into
        the payables list with nothing behind it.
        """
        from .exceptions import EmptyDocument

        if not self.lines.exists():
            raise EmptyDocument(
                f"{type(self).__name__} {self.code} has no lines. A document that moves "
                f"nothing should not reach either ledger."
            )


class PurchaseInvoice(PurchaseDocument):
    """A supplier's bill: goods into a warehouse, money owed to the vendor.

    Posting writes, in one transaction (see
    :func:`~apps.purchasing.services.post_purchase_invoice`):

    * a stock receipt per line, valued at the **line amount** rather than at a
      rate — see :class:`PurchaseInvoiceLine`;
    * Dr Inventory, Dr Tax Payable, Cr Accounts Payable, Cr Discount Received.
    """

    URL_SLUG = "invoices"

    class Meta(PurchaseDocument.Meta):
        verbose_name = "purchase invoice"
        verbose_name_plural = "purchase invoices"
        permissions = [
            ("post_purchaseinvoice", "Can post a purchase invoice to the ledger"),
            ("cancel_purchaseinvoice", "Can cancel a purchase invoice and reverse its entries"),
            ("amend_purchaseinvoice", "Can raise an amendment of a cancelled purchase invoice"),
        ]
        constraints = [
            # The supplier's own bill number, once per supplier. Entering the
            # same bill twice is the most expensive mistake available on this
            # screen: the stock arrives twice and the payable doubles. Scoped to
            # the vendor because two suppliers numbering from 1 is normal;
            # partial so that a bill with no number on it is still enterable;
            # and excluding CANCELLED so that a mistake can be cancelled and the
            # same bill entered again — which is what an amendment is.
            models.UniqueConstraint(
                fields=["vendor", "vendor_bill_no"],
                condition=~models.Q(vendor_bill_no="") & ~models.Q(status=DocumentStatus.CANCELLED),
                name="purchaseinvoice_unique_vendor_bill_no",
                violation_error_message=(
                    "That bill number has already been entered for this supplier."
                ),
            ),
        ]

    # ------------------------------------------------------------------
    # Derived figures. Nothing here is stored.
    # ------------------------------------------------------------------
    @property
    def paid_paisa(self) -> int:
        """How much has been paid against this invoice, in paisa.

        **Derived, never stored.** There is no ``paid_paisa`` column and there
        must not be one (CLAUDE.md §6): a running balance on a document header
        is a number that can disagree with the payments, and once it has
        disagreed for a week nobody can tell you which of the two is right.

        It asks :func:`apps.core.lifecycle.payment_allocations`, which resolves
        the payments app through the registry rather than importing it — so
        purchasing never depends on payments, and the same seam answers the
        identical question for a sales invoice.
        """
        from apps.core.lifecycle import payment_allocations

        return sum(allocation.amount_paisa for allocation in payment_allocations(self))

    @property
    def outstanding_paisa(self) -> int:
        """What is still owed on this invoice. Derived from :attr:`paid_paisa`."""
        return self.total_paisa - self.paid_paisa

    @property
    def is_paid(self) -> bool:
        return self.total_paisa > 0 and self.outstanding_paisa <= 0

    # ------------------------------------------------------------------
    # Lifecycle. The work is in services; these are the entry points.
    # ------------------------------------------------------------------
    def post(self, *, user=None):
        from .services import post_purchase_invoice

        return post_purchase_invoice(self, user=user)

    def cancel(self, *, user=None, reason: str = ""):
        from .services import cancel_purchase_invoice

        return cancel_purchase_invoice(self, user=user, reason=reason)

    def amend(self, *, user=None):
        from .services import amend_purchase_invoice

        return amend_purchase_invoice(self, user=user)


class PurchaseReturn(PurchaseDocument):
    """Goods going back to a supplier: stock out, money owed reduced.

    The mirror of :class:`PurchaseInvoice`, with one deliberate asymmetry that
    is not a mirror and cannot be: the goods leave at the **moving weighted
    average**, not at the rate on this document. See
    :func:`~apps.purchasing.services.post_purchase_return`.
    """

    URL_SLUG = "returns"

    class Meta(PurchaseDocument.Meta):
        verbose_name = "purchase return"
        verbose_name_plural = "purchase returns"
        permissions = [
            ("post_purchasereturn", "Can post a purchase return to the ledger"),
            ("cancel_purchasereturn", "Can cancel a purchase return and reverse its entries"),
            ("amend_purchasereturn", "Can raise an amendment of a cancelled purchase return"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["vendor", "vendor_bill_no"],
                condition=~models.Q(vendor_bill_no="") & ~models.Q(status=DocumentStatus.CANCELLED),
                name="purchasereturn_unique_vendor_bill_no",
                violation_error_message=(
                    "That credit note number has already been entered for this supplier."
                ),
            ),
        ]

    def post(self, *, user=None):
        from .services import post_purchase_return

        return post_purchase_return(self, user=user)

    def cancel(self, *, user=None, reason: str = ""):
        from .services import cancel_purchase_return

        return cancel_purchase_return(self, user=user, reason=reason)

    def amend(self, *, user=None):
        from .services import amend_purchase_return

        return amend_purchase_return(self, user=user)


# ===========================================================================
# Lines
# ===========================================================================
class PurchaseLine(models.Model):
    """One item on a purchase document, in the unit it was typed in.

    Three quantities and a rate, and the relationship between them is the whole
    reason this class has a docstring this long.

    **What the operator types** is ``qty_input`` of ``unit_input`` — "10
    cartons". **What is stored** is ``qty_base``, whole base units, because that
    is the only thing a stock row can hold (CLAUDE.md §2). The conversion is
    :func:`apps.masters.services.to_base` and it happens in :meth:`save`.

    **What the supplier bills** is ``amount_paisa``, and it is the anchor. It is
    an exact integer multiplication of what was typed — ten times the per-carton
    rate — and it is never rounded, at any point, by anything.

    ``rate_paisa`` is **derived from that amount**, not the other way round: it
    is what one base unit works out to, rounded once. Ten cartons of twelve at
    Rs 2,400 is 120 pieces at exactly Rs 200 and the two agree to the paisa; ten
    cartons of twenty-four at Rs 2,500 is 240 pieces at 1041.66... and **no
    integer rate multiplies back to the bill**. When that happens the amount is
    right and the rate is the rounded figure printed on the stock card — which
    is exactly what :class:`~apps.accounting.models.StockEntry` says about its
    own two columns, and why the stock receipt is posted at the line's value
    rather than at its rate.

    The alternative — rate first, amount from ``qty_base * rate_paisa`` — makes
    the supplier's bill wrong by up to half a paisa per piece, which on a 240
    piece line is Rs 1.20 that nobody agreed to pay.
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
            "Cost of ONE BASE UNIT, in paisa. Derived from amount_paisa and rounded once — "
            "the amount is the figure that was agreed, not this."
        ),
    )
    discount_paisa = MoneyField(
        non_negative=True,
        help_text="Discount on this line, in paisa. Posted to Discount Received.",
    )
    tax_paisa = MoneyField(
        non_negative=True,
        help_text="Sales tax on this line, in paisa. Input tax on a purchase.",
    )
    amount_paisa = MoneyField(
        non_negative=True,
        help_text=(
            "qty_input x the rate that was typed, in paisa. Exact and never rounded. "
            "This is what the supplier billed and what inventory is debited."
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
            # A discount bigger than the line turns the net cost negative, which
            # would credit inventory on a document that is putting stock in.
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
        forbids. Nothing is posted here: this is a pure function of two fields
        on the same row, and putting it anywhere else means a line saved from
        the shell or a data migration carries a ``qty_base`` that disagrees with
        its own ``qty_input``.

        The immutability check is here because ``DocumentModel`` can only guard
        its own row. A POSTED invoice whose lines could still be edited is a
        document whose ledger entries no longer describe it.
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
    def qty_display(self) -> str:
        """``"3 ctn + 5 pcs"`` — through masters, which owns that arithmetic."""
        return fmt_qty(self.item, self.qty_base)

    @property
    def rate_is_exact(self) -> bool:
        """True when ``qty_base * rate_paisa`` lands back on ``amount_paisa``.

        Not a validity check — a False here is normal and correct. It is what
        the entry screen uses to mark a line whose per-unit rate is a rounded
        figure, so nobody reads the stock card and thinks the bill is wrong.
        """
        return self.qty_base * self.rate_paisa == self.amount_paisa

    def _assert_parent_editable(self, verb: str) -> None:
        document = self.document
        if document is not None and not document.is_editable:
            raise PurchasingError(
                f"{type(document).__name__} {document.code} is {document.status}; its lines "
                f"cannot be {verb}. Cancel it and post an amendment instead."
            )


class PurchaseInvoiceLine(PurchaseLine):
    document = models.ForeignKey(
        PurchaseInvoice,
        on_delete=models.CASCADE,
        related_name="lines",
        help_text="CASCADE: a line has no meaning without its invoice, and a DRAFT may be deleted.",
    )

    class Meta(PurchaseLine.Meta):
        verbose_name = "purchase invoice line"
        verbose_name_plural = "purchase invoice lines"


class PurchaseReturnLine(PurchaseLine):
    document = models.ForeignKey(
        PurchaseReturn,
        on_delete=models.CASCADE,
        related_name="lines",
        help_text="CASCADE: a line has no meaning without its return, and a DRAFT may be deleted.",
    )

    class Meta(PurchaseLine.Meta):
        verbose_name = "purchase return line"
        verbose_name_plural = "purchase return lines"
