"""Unfold-styled admin for the chart of accounts and the ledger.

The chart is a master and is editable. The ledger is **not** — it is registered
read-only, because CLAUDE.md §3 says an entry is never updated and never
deleted, and the admin is the one place where a well-meaning person can do both
in three clicks.
"""

from django.contrib import admin
from unfold.admin import ModelAdmin

from apps.core.money import fmt

from .models import Account, LedgerEntry


@admin.register(Account)
class AccountAdmin(ModelAdmin):
    """Editable, with the structural rules enforced by ``Account.clean()``.

    Deleting an account that has entries is impossible regardless of what is
    clicked here: the ledger's foreign key is ``PROTECT``.
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
