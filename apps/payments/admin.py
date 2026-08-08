"""Unfold-styled admin for receipts, payments and cheque events.

Register with ``unfold.admin.ModelAdmin``, not ``django.contrib.admin.ModelAdmin``.

Same shape and same role as the sales and purchasing admins: this is the back
door. Money is taken on the recovery workspace at ``/payments/recovery/``, which
is the screen this whole app is really for. What this page is good at is looking
things up — by party, by route, by cheque number, by what is still on account —
and running the lifecycle actions, which are wired to the **services** and never
to a form save.

Two things are deliberately not offered here, both for the same reason.
**Clearing and bouncing** are not batch actions: "the bank sent these forty
cheques back" is not a sentence anybody says, and a checkbox-selected bounce is
a reversal nobody looked at. **Allocation** is not an inline: it is edited
against the shop's open bills, on the screen that shows them.
"""

from django.contrib import admin, messages
from django.utils.html import format_html
from unfold.admin import ModelAdmin, TabularInline

from apps.core.enums import DocumentStatus
from apps.core.exceptions import CoreError
from apps.core.money import fmt

from .enums import ChequeStatus, PaymentMode
from .models import ChequeEvent, Payment, PaymentAllocation
from .services import attach_parties, cancel_cheque_event, cancel_payment, post_payment

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


class PaymentAllocationInline(TabularInline):
    """What a payment settles, read-only.

    Read-only here on purpose. An allocation has to be checked against the
    document it names — the right party, posted, and with room left on it — and
    those checks live in :func:`apps.payments.services.allocate_payment`, which
    the recovery workspace calls. A raw row typed in here would skip all three.
    """

    model = PaymentAllocation
    extra = 0
    can_delete = False
    fields = ("invoice_type", "invoice_id", "document", "amount")
    readonly_fields = ("invoice_type", "invoice_id", "document", "amount")

    def has_add_permission(self, request, obj=None):
        return False

    @admin.display(description="Document")
    def document(self, obj) -> str:
        return obj.document_code

    @admin.display(description="Applied")
    def amount(self, obj) -> str:
        return fmt(obj.amount_paisa)


class ChequeEventInline(TabularInline):
    """What the bank did. Read-only; the actions are on the payment screen."""

    model = ChequeEvent
    extra = 0
    can_delete = False
    fields = ("code", "kind", "posting_date", "status", "remarks")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Payment)
class PaymentAdmin(ModelAdmin):
    """Money in and money out. Entry lives on the workspace; this is lookup."""

    list_display = (
        "code",
        "posting_date",
        "party",
        "direction",
        "mode",
        "route",
        "collected_by",
        "amount",
        "on_account",
        "cheque",
        "status_badge",
    )
    list_filter = (
        "status",
        "direction",
        "mode",
        "posting_date",
        "route",
        "collected_by",
        "party_type",
    )
    search_fields = ("code", "cheque_no", "bank_name", "remarks")
    date_hierarchy = "posting_date"
    ordering = ("-posting_date", "-id")
    list_select_related = ("route", "collected_by")
    autocomplete_fields = ("route", "collected_by")
    inlines = (PaymentAllocationInline, ChequeEventInline)
    list_per_page = 50
    actions = ("post_selected", "cancel_selected")

    fieldsets = (
        (None, {"fields": ("code", "status", "direction", "mode", "amount_paisa")}),
        (
            "Party",
            {
                "fields": ("party_type", "party_id"),
                "description": (
                    "A soft (type, id) pair, exactly as the ledger carries it — no foreign "
                    "key, so a ledger row outlives the master it points at."
                ),
            },
        ),
        (
            "Cheque",
            {
                "fields": ("cheque_no", "cheque_date", "bank_name"),
                "description": (
                    "A cheque posts to Cheques in Hand, not to Bank. Clearing and bouncing "
                    "are separate postings on their own dates — see the cheque events below."
                ),
            },
        ),
        (
            "Recovery",
            {
                "fields": ("posting_date", "route", "collected_by"),
                "description": "Both default from the client and both can be overridden.",
            },
        ),
        ("Notes", {"fields": ("remarks",)}),
        ("Lifecycle", {"fields": AUDIT_FIELDS[2:], "classes": ["collapse"]}),
    )

    def get_queryset(self, request):
        return super().get_queryset(request).with_cheque_status().with_allocated()

    @admin.display(description="Party")
    def party(self, obj) -> str:
        return obj.party_name

    @admin.display(description="Amount", ordering="amount_paisa")
    def amount(self, obj) -> str:
        return fmt(obj.amount_paisa)

    @admin.display(description="On account")
    def on_account(self, obj) -> str:
        """Derived from the allocation rows, never stored — CLAUDE.md §6."""
        return fmt(obj.unallocated_paisa) if obj.unallocated_paisa else "—"

    @admin.display(description="Cheque")
    def cheque(self, obj) -> str:
        if obj.mode != PaymentMode.CHEQUE:
            return "—"
        colour = {
            ChequeStatus.PENDING: "#6b7280",
            ChequeStatus.CLEARED: "#15803d",
            ChequeStatus.BOUNCED: "#b91c1c",
        }[obj.cheque_status]
        return format_html(
            '{} <span style="color:{};font-weight:600">{}</span>',
            obj.cheque_no,
            colour,
            obj.cheque_status_label,
        )

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
        readonly = list(AUDIT_FIELDS)
        if obj is not None and not obj.is_editable:
            readonly += [
                field.name
                for field in self.model._meta.fields
                if field.editable and field.name not in readonly
            ]
        return tuple(dict.fromkeys(readonly))

    def has_delete_permission(self, request, obj=None):
        """A DRAFT may be deleted; anything that has touched the ledger may not."""
        if obj is None:
            return True
        return obj.is_editable

    def changelist_view(self, request, extra_context=None):
        """Resolve the soft party link for the page in one query per type."""
        response = super().changelist_view(request, extra_context)
        results = getattr(
            getattr(response, "context_data", {}).get("cl", None), "result_list", None
        )
        if results is not None:
            attach_parties(results)
        return response

    # ------------------------------------------------------------------
    # Actions. Both go through the service.
    # ------------------------------------------------------------------
    def _run(self, request, queryset, operation, verb: str) -> None:
        done, failed = 0, 0
        for payment in queryset:
            try:
                operation(payment, user=request.user)
                done += 1
            except CoreError as exc:
                failed += 1
                self.message_user(request, f"{payment.code}: {exc}", level=messages.ERROR)
        if done:
            self.message_user(request, f"{verb} {done} payment(s).", level=messages.SUCCESS)
        if not done and not failed:
            self.message_user(request, "Nothing selected.", level=messages.WARNING)

    @admin.action(description="Post selected payments")
    def post_selected(self, request, queryset):
        self._run(request, queryset.filter(status=DocumentStatus.DRAFT), post_payment, "Posted")

    @admin.action(description="Cancel selected payments")
    def cancel_selected(self, request, queryset):
        def cancel(payment, *, user):
            return cancel_payment(payment, user=user, reason="Cancelled from the admin")

        self._run(request, queryset.filter(status=DocumentStatus.POSTED), cancel, "Cancelled")


@admin.register(ChequeEvent)
class ChequeEventAdmin(ModelAdmin):
    """What the bank did with a cheque, and when.

    Recording one is done from the payment screen, where the cheque and its
    amount are in front of you. This page is for finding them afterwards — "when
    did that Meezan cheque bounce" is a question somebody asks a month later.
    """

    list_display = ("code", "posting_date", "payment", "kind_badge", "amount", "status_badge")
    list_filter = ("status", "kind", "posting_date")
    search_fields = ("code", "payment__code", "payment__cheque_no", "remarks")
    date_hierarchy = "posting_date"
    ordering = ("-posting_date", "-id")
    list_select_related = ("payment",)
    autocomplete_fields = ("payment",)
    actions = ("cancel_selected",)

    fieldsets = (
        (None, {"fields": ("code", "status", "payment", "kind", "posting_date")}),
        ("Notes", {"fields": ("remarks",)}),
        ("Lifecycle", {"fields": AUDIT_FIELDS[2:], "classes": ["collapse"]}),
    )

    @admin.display(description="Amount")
    def amount(self, obj) -> str:
        return fmt(obj.payment.amount_paisa)

    @admin.display(description="What happened", ordering="kind")
    def kind_badge(self, obj) -> str:
        colour = "#b91c1c" if obj.is_bounce else "#15803d"
        return format_html(
            '<span style="color:{};font-weight:600">{}</span>', colour, obj.get_kind_display()
        )

    @admin.display(description="Status", ordering="status")
    def status_badge(self, obj) -> str:
        return obj.get_status_display()

    def get_readonly_fields(self, request, obj=None):
        readonly = list(AUDIT_FIELDS)
        if obj is not None and not obj.is_editable:
            readonly += [
                field.name
                for field in self.model._meta.fields
                if field.editable and field.name not in readonly
            ]
        return tuple(dict.fromkeys(readonly))

    def has_delete_permission(self, request, obj=None):
        if obj is None:
            return True
        return obj.is_editable

    @admin.action(description="Reverse selected cheque events")
    def cancel_selected(self, request, queryset):
        done = 0
        for event in queryset.filter(status=DocumentStatus.POSTED):
            try:
                cancel_cheque_event(event, user=request.user, reason="Reversed from the admin")
                done += 1
            except CoreError as exc:
                self.message_user(request, f"{event.code}: {exc}", level=messages.ERROR)
        if done:
            self.message_user(request, f"Reversed {done} event(s).", level=messages.SUCCESS)
