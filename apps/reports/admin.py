"""Unfold-styled admin for the company profile.

Register with ``unfold.admin.ModelAdmin``, not ``django.contrib.admin.ModelAdmin``.

There is one row and there is only ever one row, so this admin is shaped around
editing it rather than around a list: adding is refused once it exists, deleting
is refused outright, and the changelist is a door to the single change form.
"""

from django.contrib import admin
from django.shortcuts import redirect
from django.urls import reverse
from unfold.admin import ModelAdmin

from .models import SINGLETON_PK, CompanyProfile


@admin.register(CompanyProfile)
class CompanyProfileAdmin(ModelAdmin):
    """The details printed at the top of every document.

    The logo is a ``FileField`` rather than a URL and there is nowhere to type
    one: CLAUDE.md §7 — the office PC has no internet, so a hotlinked logo is a
    blank box on every invoice.
    """

    readonly_fields = ("created_at", "created_by", "updated_at", "updated_by")
    fieldsets = (
        (
            None,
            {
                "fields": ("name", "logo"),
                "description": (
                    "The logo is uploaded and stored on this machine. Roughly 600x200 "
                    "prints well; JPEG or PNG only."
                ),
            },
        ),
        ("Contact", {"fields": ("address", "phone", "email")}),
        (
            "Tax registration",
            {
                "fields": ("ntn", "strn"),
                "description": "Printed on every invoice. Leave blank if unregistered.",
            },
        ),
        (
            "Printed text",
            {
                "fields": ("footer_text", "invoice_terms"),
                "description": (
                    "The footer prints on every page above the page number; the terms print "
                    "once, under the totals of an invoice."
                ),
            },
        ),
        ("Audit", {"fields": readonly_fields, "classes": ["collapse"]}),
    )

    def has_add_permission(self, request):
        """Only when the row does not exist yet — see CompanyProfile.save()."""
        return not CompanyProfile.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        """Straight to the one row. A list of one is a page nobody wants.

        ``CompanyProfile.get()`` creates it empty on first open, so a fresh
        installation lands on a form rather than on an "add" button it has to
        find.
        """
        CompanyProfile.get()
        return redirect(reverse("admin:reports_companyprofile_change", args=[SINGLETON_PK]))
