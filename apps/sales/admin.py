"""Unfold-styled admin for the sales documents.

Register with ``unfold.admin.ModelAdmin``, not ``django.contrib.admin.ModelAdmin``.

Same shape as the purchasing admin, and the same role: this is the back door.
Entry happens on the keyboard screen at ``/sales/invoices/new/``, which is the
screen this whole system is really for. What this page is good at is looking
things up — by client, by route, by seller, by due date — and running the two
lifecycle actions, which are wired to the **services** and never to a form save.

One difference from purchasing, and it is deliberate: posting from here does
**not** offer the credit-limit override. Overriding a limit is a decision
somebody makes about one named client with the figures in front of them, not
something to be applied to a checkbox-selected batch of forty invoices.
"""

from django.contrib import admin, messages
from django.utils.html import format_html
from unfold.admin import ModelAdmin, TabularInline

from apps.core.enums import DocumentStatus
from apps.core.exceptions import CoreError
from apps.core.money import fmt

from .models import SalesInvoice, SalesInvoiceLine, SalesReturn, SalesReturnLine
from .services import (
    cancel_sales_invoice,
    cancel_sales_return,
    post_sales_invoice,
    post_sales_return,
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


class SalesLineInline(TabularInline):
    """Lines, read-only in every column that is derived.

    ``cogs_paisa`` in particular: it is captured from the stock ledger at post
    time and typing over it would put a margin on the books that no stock
    movement ever backed.
    """

    extra = 0
    autocomplete_fields = ("item",)
    fields = ("item", "qty_input", "unit_input", "qty_base", "rate", "amount", "tax", "cogs")
    readonly_fields = ("qty_base", "rate", "amount", "tax", "cogs")

    @admin.display(description="Rate / base unit")
    def rate(self, obj) -> str:
        return fmt(obj.rate_paisa)

    @admin.display(description="Amount")
    def amount(self, obj) -> str:
        return fmt(obj.amount_paisa)

    @admin.display(description="Tax")
    def tax(self, obj) -> str:
        return fmt(obj.tax_paisa)

    @admin.display(description="Cost (captured)")
    def cogs(self, obj) -> str:
        return fmt(obj.cogs_paisa) if obj.cogs_paisa else "—"


class SalesInvoiceLineInline(SalesLineInline):
    model = SalesInvoiceLine


class SalesReturnLineInline(SalesLineInline):
    model = SalesReturnLine


class SalesDocumentAdmin(ModelAdmin):
    """Everything the invoice and the credit note admin share."""

    list_display = (
        "code",
        "posting_date",
        "client",
        "route",
        "seller",
        "warehouse",
        "subtotal",
        "tax",
        "total",
        "status_badge",
    )
    list_filter = ("status", "posting_date", "route", "seller", "warehouse")
    search_fields = ("code", "client__code", "client__name", "client__phone", "remarks")
    date_hierarchy = "posting_date"
    ordering = ("-posting_date", "-id")
    list_select_related = ("client", "route", "seller", "warehouse")
    autocomplete_fields = ("client", "route", "seller")
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


@admin.register(SalesInvoice)
class SalesInvoiceAdmin(SalesDocumentAdmin):
    """A shop's bill. Entry lives on the keyboard screen; this is lookup."""

    inlines = (SalesInvoiceLineInline,)
    _cancel = staticmethod(cancel_sales_invoice)

    list_display = (*SalesDocumentAdmin.list_display, "due_date", "outstanding")
    list_filter = (*SalesDocumentAdmin.list_filter, "due_date")

    fieldsets = (
        (None, {"fields": ("code", "status", "client", "warehouse")}),
        (
            "Beat",
            {
                "fields": ("route", "seller"),
                "description": (
                    "Both default from the client and both can be overridden — a booker "
                    "sometimes covers another route."
                ),
            },
        ),
        ("Dates", {"fields": ("posting_date", "due_date")}),
        (
            "Totals",
            {
                "fields": TOTAL_FIELDS,
                "description": (
                    "Recomputed from the lines, in paisa. A display convenience — every "
                    "receivable is aggregated from the ledger, never read from here."
                ),
            },
        ),
        ("Notes", {"fields": ("remarks",)}),
        ("Lifecycle", {"fields": AUDIT_FIELDS[2:], "classes": ["collapse"]}),
    )

    @staticmethod
    def _post(document, *, user):
        """No override from here. See the module docstring."""
        return post_sales_invoice(document, user=user, override_credit_limit=False)

    @admin.display(description="Outstanding")
    def outstanding(self, obj) -> str:
        """Derived from the receipts, never stored — see ``SalesInvoice.paid_paisa``."""
        return fmt(obj.outstanding_paisa)


@admin.register(SalesReturn)
class SalesReturnAdmin(SalesDocumentAdmin):
    """A credit note. The invoice's mirror."""

    inlines = (SalesReturnLineInline,)
    _post = staticmethod(post_sales_return)
    _cancel = staticmethod(cancel_sales_return)

    list_display = (*SalesDocumentAdmin.list_display, "against")
    autocomplete_fields = (*SalesDocumentAdmin.autocomplete_fields, "against_invoice")

    fieldsets = (
        (None, {"fields": ("code", "status", "client", "warehouse", "against_invoice")}),
        ("Beat", {"fields": ("route", "seller")}),
        ("Dates", {"fields": ("posting_date",)}),
        (
            "Totals",
            {
                "fields": TOTAL_FIELDS,
                "description": (
                    "Recomputed from the lines, in paisa. The goods come back into stock at "
                    "what they cost when they left, which is not any of these figures."
                ),
            },
        ),
        ("Notes", {"fields": ("remarks",)}),
        ("Lifecycle", {"fields": AUDIT_FIELDS[2:], "classes": ["collapse"]}),
    )

    @admin.display(description="Against", ordering="against_invoice__code")
    def against(self, obj) -> str:
        return obj.against_invoice.code if obj.against_invoice_id else "—"
