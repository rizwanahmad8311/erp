"""Unfold-styled admin for core infrastructure."""

from django.contrib import admin
from unfold.admin import ModelAdmin

from .models import DocumentSequence


@admin.register(DocumentSequence)
class DocumentSequenceAdmin(ModelAdmin):
    """Visible for support, but not editable.

    Editing ``last_number`` by hand either hands out a duplicate code or skips a
    block of numbers. Both are the kind of thing that is discovered a month
    later, so the admin is read-only and allocation goes through
    ``apps.core.services.get_next_code``.
    """

    list_display = ("prefix", "fiscal_year", "last_number", "updated_at")
    list_filter = ("prefix", "fiscal_year")
    search_fields = ("prefix",)
    ordering = ("prefix", "fiscal_year")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
