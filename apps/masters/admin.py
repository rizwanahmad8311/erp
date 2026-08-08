"""Unfold-styled admin registrations for this app.

Register with unfold.admin.ModelAdmin, not django.contrib.admin.ModelAdmin.
"""

from django.contrib import admin
from unfold.admin import ModelAdmin

from .models import Item


@admin.register(Item)
class ItemAdmin(ModelAdmin):
    """Editable master.

    Deleting an item that has stock movement is impossible regardless of what
    is clicked here: ``StockEntry.item`` is ``PROTECT``.
    """

    list_display = ("code", "name")
    search_fields = ("code", "name")
    ordering = ("code",)
    readonly_fields = ("created_at", "updated_at", "created_by", "updated_by")
    fieldsets = (
        (None, {"fields": ("code", "name")}),
        ("Audit", {"fields": ("created_at", "created_by", "updated_at", "updated_by")}),
    )
