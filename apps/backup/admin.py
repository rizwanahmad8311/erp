"""Unfold-styled admin registrations for this app.

Register with unfold.admin.ModelAdmin, not django.contrib.admin.ModelAdmin.

``BackupLog`` is registered **read-only**, for the same reason the two ledgers
are (CLAUDE.md §3): it is a record of what happened. A row that can be edited is
a record of what somebody wanted to have happened, and "the backup succeeded" is
exactly the sentence nobody should be able to type.

The screen people actually use is at ``/backup/`` — see :mod:`apps.backup.views`.
This is here so an administrator can read the log next to everything else.
"""

from django.contrib import admin
from unfold.admin import ModelAdmin

from .models import BackupLog


@admin.register(BackupLog)
class BackupLogAdmin(ModelAdmin):
    list_display = ("created_at", "run_id", "destination", "outcome", "size_display", "filename")
    list_filter = ("destination", "outcome")
    search_fields = ("run_id", "filename", "sha256")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
