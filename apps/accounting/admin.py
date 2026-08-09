"""Unfold-styled admin for the chart of accounts, the warehouses, and the two
ledgers.

The chart and the warehouse list are masters and are editable. The ledgers are
**not** — they are registered read-only, because CLAUDE.md §3 says an entry is
never updated and never deleted, and the admin is the one place where a
well-meaning person can do both in three clicks.
"""

from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin
from unfold.admin import ModelAdmin

from apps.core.money import fmt

from .models import Account, FiscalYearClose, LedgerEntry, StockEntry, Warehouse


@admin.register(Account)
class AccountAdmin(SimpleHistoryAdmin, ModelAdmin):
    """Editable, with the structural rules enforced by ``Account.clean()``.

    Deleting an account that has entries is impossible regardless of what is
    clicked here: the ledger's foreign key is ``PROTECT``.

    ``SimpleHistoryAdmin`` first and Unfold's ``ModelAdmin`` second, so the
    History button wins and the Unfold templates still apply — the same order
    ``apps.masters.admin.HistoryModelAdmin`` uses, and for the same reason. The
    two ledgers below get no history screen and must not: they are append-only
    and a row that never changes has no history to show.
    """

    list_display = ("code", "name", "type", "parent", "is_group", "is_active")
    list_filter = ("type", "is_group", "is_active")
    search_fields = ("code", "name")
    ordering = ("code",)
    list_select_related = ("parent",)
    autocomplete_fields = ("parent",)
    readonly_fields = ("created_at", "updated_at", "created_by", "updated_by")
    fieldsets = (
        (None, {"fields": ("code", "name", "type")}),
        ("Position in the chart", {"fields": ("parent", "is_group")}),
        ("Status", {"fields": ("is_active",)}),
        ("Audit", {"fields": ("created_at", "created_by", "updated_at", "updated_by")}),
    )


@admin.register(LedgerEntry)
class LedgerEntryAdmin(ModelAdmin):
    """Read-only. Every permission hook says no, on purpose.

    Corrections are made by cancelling the document, which posts reversing rows
    through ``accounting.services.reverse_entries``. There is no route from this
    page to a changed number, and there must not be one.
    """

    list_display = (
        "posting_date",
        "account",
        "debit",
        "credit",
        "voucher_code",
        "party",
        "is_reversal",
    )
    list_filter = ("posting_date", "is_reversal", "voucher_type", "party_type", "account__type")
    search_fields = ("voucher_code", "remarks", "account__code", "account__name")
    date_hierarchy = "posting_date"
    ordering = ("-posting_date", "-id")
    list_select_related = ("account",)

    @admin.display(description="Debit", ordering="debit_paisa")
    def debit(self, obj) -> str:
        return fmt(obj.debit_paisa) if obj.debit_paisa else ""

    @admin.display(description="Credit", ordering="credit_paisa")
    def credit(self, obj) -> str:
        return fmt(obj.credit_paisa) if obj.credit_paisa else ""

    @admin.display(description="Party")
    def party(self, obj) -> str:
        return f"{obj.get_party_type_display()} #{obj.party_id}" if obj.party_type else ""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Warehouse)
class WarehouseAdmin(ModelAdmin):
    """Editable, with the one-default rule enforced by ``Warehouse.clean()``.

    Deleting a warehouse that has movement is impossible regardless of what is
    clicked here: the stock ledger's foreign key is ``PROTECT``.
    """

    list_display = ("code", "name", "is_default")
    list_filter = ("is_default",)
    search_fields = ("code", "name")
    ordering = ("code",)
    readonly_fields = ("created_at", "updated_at", "created_by", "updated_by")
    fieldsets = (
        (None, {"fields": ("code", "name")}),
        ("Status", {"fields": ("is_default",)}),
        ("Audit", {"fields": ("created_at", "created_by", "updated_at", "updated_by")}),
    )


@admin.register(StockEntry)
class StockEntryAdmin(ModelAdmin):
    """Read-only. Every permission hook says no, on purpose.

    Corrections are made by cancelling the document, which posts reversing rows
    through ``accounting.services.reverse_stock``. There is no route from this
    page to a changed quantity, and there must not be one.
    """

    list_display = (
        "posting_date",
        "item",
        "warehouse",
        "qty_base",
        "rate",
        "value",
        "voucher_code",
        "is_reversal",
    )
    list_filter = ("posting_date", "is_reversal", "warehouse", "voucher_type")
    search_fields = ("voucher_code", "item__code", "item__name", "warehouse__code")
    date_hierarchy = "posting_date"
    ordering = ("-posting_date", "-id")
    list_select_related = ("item", "warehouse")

    @admin.display(description="Rate", ordering="rate_paisa")
    def rate(self, obj) -> str:
        return fmt(obj.rate_paisa)

    @admin.display(description="Value", ordering="value_paisa")
    def value(self, obj) -> str:
        return fmt(obj.value_paisa)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(FiscalYearClose)
class FiscalYearCloseAdmin(ModelAdmin):
    """Read-mostly. The close is posted by `manage.py close_fiscal_year`.

    Adding one here would let somebody create a header with no ledger entries
    behind it, which is exactly the state ``check_integrity`` exists to find.
    Cancelling goes through the shared cancel screen like every other document.
    """

    list_display = ("code", "fiscal_year", "posting_date", "status", "profit_display")
    list_filter = ("status", "fiscal_year")
    search_fields = ("code",)
    ordering = ("-fiscal_year",)
    readonly_fields = (
        "code",
        "fiscal_year",
        "posting_date",
        "profit_paisa",
        "status",
        "posted_at",
        "posted_by",
        "cancelled_at",
        "cancelled_by",
        "cancel_reason",
    )

    @admin.display(description="Profit", ordering="profit_paisa")
    def profit_display(self, obj) -> str:
        from apps.core.money import fmt

        return fmt(obj.profit_paisa)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
