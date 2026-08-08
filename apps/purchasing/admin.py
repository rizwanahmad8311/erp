"""Unfold-styled admin for the purchasing documents.

Register with ``unfold.admin.ModelAdmin``, not ``django.contrib.admin.ModelAdmin``.

The admin is the back door, not the front one: entry happens on the keyboard
screen at ``/purchasing/invoices/new/``. What this is for is looking things up,
and for the two lifecycle actions — post and cancel — which are wired to the
**services**, never to a form save. Nothing on this page writes a ledger row of
its own.

A POSTED or CANCELLED document is read-only here, because ``DocumentModel`` will
raise ``DocumentImmutable`` on any change to one and a form that offers fields it
cannot save is a form that wastes somebody's afternoon.
"""

from django.contrib import admin, messages
from django.utils.html import format_html
from unfold.admin import ModelAdmin, TabularInline

from apps.core.enums import DocumentStatus
from apps.core.exceptions import CoreError
from apps.core.money import fmt

from .models import (
    PurchaseInvoice,
    PurchaseInvoiceLine,
    PurchaseReturn,
    PurchaseReturnLine,
)
from .services import (
    cancel_purchase_invoice,
    cancel_purchase_return,
    post_purchase_invoice,
    post_purchase_return,
)

AUDIT_FIELDS = (
    "code",
    "status",
    "posted_at",
    "posted_by",
    "cancelled_at",
    "cancelled_by",
    "cancel_reason",
    "amended_from",
    "amendment_no",
    "created_at",
    "created_by",
    "updated_at",
    "updated_by",
)

#: Recomputed from the lines by ``recalculate_totals``. Never typed in.
TOTAL_FIELDS = ("subtotal_paisa", "discount_paisa", "tax_paisa", "total_paisa")


class PurchaseLineInline(TabularInline):
    """Lines, read-only in every column that is derived.

    ``qty_base``, ``rate_paisa`` and ``amount_paisa`` come out of
    ``services.compute_line`` — typing over them by hand is how a document ends
    up with a quantity that disagrees with its own amount.
    """

    extra = 0
    autocomplete_fields = ("item",)
    fields = (
        "item",
        "qty_input",
        "unit_input",
        "qty_base",
        "rate",
        "amount",
        "discount",
        "tax",
    )
    readonly_fields = ("qty_base", "rate", "amount", "discount", "tax")

    @admin.display(description="Rate / base unit")
    def rate(self, obj) -> str:
        return fmt(obj.rate_paisa)

    @admin.display(description="Amount")
    def amount(self, obj) -> str:
        return fmt(obj.amount_paisa)

    @admin.display(description="Discount")
    def discount(self, obj) -> str:
        return fmt(obj.discount_paisa)

    @admin.display(description="Tax")
    def tax(self, obj) -> str:
        return fmt(obj.tax_paisa)


class PurchaseInvoiceLineInline(PurchaseLineInline):
    model = PurchaseInvoiceLine


class PurchaseReturnLineInline(PurchaseLineInline):
    model = PurchaseReturnLine


class PurchaseDocumentAdmin(ModelAdmin):
    """Everything the invoice and the return admin share.

    Both are frozen once posted, both expose the same two actions, and both call
    a service to do the work.
    """

    list_display = (
        "code",
        "posting_date",
        "vendor",
        "warehouse",
        "vendor_bill_no",
        "subtotal",
        "tax",
        "total",
        "status_badge",
    )
    list_filter = ("status", "posting_date", "warehouse", "vendor")
    search_fields = ("code", "vendor_bill_no", "vendor__code", "vendor__name", "remarks")
    date_hierarchy = "posting_date"
    ordering = ("-posting_date", "-id")
    list_select_related = ("vendor", "warehouse")
    autocomplete_fields = ("vendor",)
    list_per_page = 50
    actions = ("post_selected", "cancel_selected")

    @admin.display(description="Subtotal", ordering="subtotal_paisa")
    def subtotal(self, obj) -> str:
        return fmt(obj.subtotal_paisa)

    @admin.display(description="Tax", ordering="tax_paisa")
    def tax(self, obj) -> str:
        return fmt(obj.tax_paisa)

    @admin.display(description="Total", ordering="total_paisa")
    def total(self, obj) -> str:
        return fmt(obj.total_paisa)

    @admin.display(description="Status", ordering="status")
    def status_badge(self, obj) -> str:
        colour = {
            DocumentStatus.DRAFT: "#6b7280",
            DocumentStatus.POSTED: "#15803d",
            DocumentStatus.CANCELLED: "#b91c1c",
        }[obj.status]
        return format_html(
            '<span style="color:{};font-weight:600">{}</span>', colour, obj.get_status_display()
        )

    # ------------------------------------------------------------------
    # A posted document is frozen
    # ------------------------------------------------------------------
    def get_readonly_fields(self, request, obj=None):
        readonly = [*AUDIT_FIELDS, *TOTAL_FIELDS]
        if obj is not None and not obj.is_editable:
            readonly += [
                field.name
                for field in self.model._meta.fields
                if field.editable and field.name not in readonly
            ]
        return tuple(dict.fromkeys(readonly))

    def has_delete_permission(self, request, obj=None):
        """A DRAFT may be deleted; anything that has touched a ledger may not."""
        if obj is None:
            return True
        return obj.is_editable

    def get_inlines(self, request, obj):
        return self.inlines

    def get_formsets_with_inlines(self, request, obj=None):
        for formset, inline in super().get_formsets_with_inlines(request, obj):
            if obj is not None and not obj.is_editable:
                formset.max_num = 0
                formset.extra = 0
            yield formset, inline

    # ------------------------------------------------------------------
    # Actions. Both go through the service.
    # ------------------------------------------------------------------
    def _run(self, request, queryset, operation, verb: str) -> None:
        done, failed = 0, 0
        for document in queryset:
            try:
                operation(document, user=request.user)
                done += 1
            except CoreError as exc:
                failed += 1
                self.message_user(request, f"{document.code}: {exc}", level=messages.ERROR)
        if done:
            self.message_user(request, f"{verb} {done} document(s).", level=messages.SUCCESS)
        if not done and not failed:
            self.message_user(request, "Nothing selected.", level=messages.WARNING)

    @admin.action(description="Post selected documents")
    def post_selected(self, request, queryset):
        self._run(request, queryset.filter(status=DocumentStatus.DRAFT), self._post, "Posted")

    @admin.action(description="Cancel selected documents")
    def cancel_selected(self, request, queryset):
        def cancel(document, *, user):
            return self._cancel(document, user=user, reason="Cancelled from the admin")

        self._run(request, queryset.filter(status=DocumentStatus.POSTED), cancel, "Cancelled")


@admin.register(PurchaseInvoice)
class PurchaseInvoiceAdmin(PurchaseDocumentAdmin):
    """A supplier's bill. Entry lives on the keyboard screen; this is lookup."""

    inlines = (PurchaseInvoiceLineInline,)
    _post = staticmethod(post_purchase_invoice)
    _cancel = staticmethod(cancel_purchase_invoice)

    list_display = (*PurchaseDocumentAdmin.list_display, "outstanding")

    fieldsets = (
        (None, {"fields": ("code", "status", "vendor", "warehouse", "posting_date")}),
        ("Supplier's bill", {"fields": ("vendor_bill_no", "vendor_bill_date")}),
        (
            "Totals",
            {
                "fields": TOTAL_FIELDS,
                "description": (
                    "Recomputed from the lines, in paisa. A display convenience — every "
                    "payables figure is aggregated from the ledger, never read from here."
                ),
            },
        ),
        ("Notes", {"fields": ("remarks",)}),
        ("Lifecycle", {"fields": AUDIT_FIELDS[2:], "classes": ["collapse"]}),
    )

    @admin.display(description="Outstanding")
    def outstanding(self, obj) -> str:
        """Derived from the payments, never stored — see ``PurchaseInvoice.paid_paisa``."""
        return fmt(obj.outstanding_paisa)


@admin.register(PurchaseReturn)
class PurchaseReturnAdmin(PurchaseDocumentAdmin):
    """Goods back to a supplier. The invoice's mirror."""

    inlines = (PurchaseReturnLineInline,)
    _post = staticmethod(post_purchase_return)
    _cancel = staticmethod(cancel_purchase_return)

    fieldsets = (
        (None, {"fields": ("code", "status", "vendor", "warehouse", "posting_date")}),
        ("Supplier's credit note", {"fields": ("vendor_bill_no", "vendor_bill_date")}),
        (
            "Totals",
            {
                "fields": TOTAL_FIELDS,
                "description": (
                    "Recomputed from the lines, in paisa. The goods leave at the moving "
                    "average, which is what Inventory is credited — not these figures."
                ),
            },
        ),
        ("Notes", {"fields": ("remarks",)}),
        ("Lifecycle", {"fields": AUDIT_FIELDS[2:], "classes": ["collapse"]}),
    )
