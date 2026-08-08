"""
Abstract base models. Nothing here creates a table — there are no business
models in this project yet, by design.

The bases exist so the locked rules in CLAUDE.md are enforced by inheritance
rather than by everyone remembering them.
"""

from django.conf import settings
from django.db import models

from .enums import ALLOWED_STATUS_TRANSITIONS, DocumentStatus


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class AppendOnlyModel(models.Model):
    """Base for ledger and stock rows: insert only, forever.

    A row that has been written is history. Corrections are new reversing rows,
    never an UPDATE and never a DELETE. Both operations raise here so the rule
    fails loudly in tests instead of quietly in a year-end report.
    """

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise AppendOnlyViolation(
                f"{type(self).__name__} is append-only; write a reversing row "
                f"instead of updating pk={self.pk}."
            )
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise AppendOnlyViolation(
            f"{type(self).__name__} is append-only; write a reversing row "
            f"instead of deleting pk={self.pk}."
        )


class AppendOnlyViolation(Exception):
    """Raised on any attempt to UPDATE or DELETE an append-only row."""


class DocumentModel(TimeStampedModel):
    """Base for every posted document: invoices, bills, receipts, adjustments.

    Concrete subclasses add their own number/party/line relations. They do not
    add cached totals that reports read — reports read the ledger.
    """

    status = models.CharField(
        max_length=16,
        choices=DocumentStatus.choices,
        default=DocumentStatus.DRAFT,
        db_index=True,
    )
    posted_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    amended_from = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="%(app_label)s_%(class)s_amendments",
        help_text="The cancelled document this one replaces.",
    )

    class Meta:
        abstract = True

    @property
    def is_editable(self) -> bool:
        return self.status == DocumentStatus.DRAFT

    def assert_transition(self, new_status: str) -> None:
        """Guard the lifecycle. Call before mutating status in a posting service."""
        allowed = ALLOWED_STATUS_TRANSITIONS.get(self.status, set())
        if new_status not in allowed:
            raise IllegalTransition(
                f"{type(self).__name__} pk={self.pk}: cannot move {self.status} -> {new_status}."
            )

    def assert_editable(self) -> None:
        if not self.is_editable:
            raise ImmutableDocument(
                f"{type(self).__name__} pk={self.pk} is {self.status} and "
                f"cannot be modified. Cancel it and post an amendment."
            )


class IllegalTransition(Exception):
    """Raised on a status change outside DRAFT -> POSTED -> CANCELLED."""


class ImmutableDocument(Exception):
    """Raised on an attempt to edit a POSTED or CANCELLED document."""
