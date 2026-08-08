"""
Concrete models that exist **only under pytest**, so the abstract bases in
apps.core can be exercised against a real table.

This app is added to INSTALLED_APPS by config/settings/test.py and by nothing
else. These are not domain models and must never be imported by application
code — when the real sales invoice arrives it inherits from DocumentModel
directly, and these stay here as the base class's test harness.
"""

from django.db import models, transaction

from apps.core.enums import DocumentStatus
from apps.core.fields import MoneyField, QuantityField
from apps.core.models import AppendOnlyModel, DocumentModel


class SampleEntry(AppendOnlyModel):
    """Stand-in for LedgerEntry/StockEntry: proves the append-only guard bites."""

    document_code = models.CharField(max_length=32, db_index=True)
    amount_paisa = MoneyField()
    qty_pieces = QuantityField()
    is_reversal = models.BooleanField(default=False)


class SampleDocument(DocumentModel):
    """Minimal concrete document.

    ``post`` and ``cancel`` write SampleEntry rows the way a real posting
    service would: inside ``transaction.atomic()``, and cancelling writes
    *reversing* rows rather than touching what is already there.
    """

    party_name = models.CharField(max_length=100, default="")
    note = models.TextField(blank=True, default="")

    class Meta:
        # Every concrete document declares its own cancel permission — see
        # DocumentModel.cancel_permission(). Declared here too so the harness
        # obeys the same contract the audit in tests/test_lifecycle.py enforces.
        permissions = [("cancel_sampledocument", "Can cancel a sample document")]

    @transaction.atomic
    def post(self, *, user=None, **options):
        self.assert_transition(DocumentStatus.POSTED)
        for line in self.lines.all():
            SampleEntry.objects.create(
                document_code=self.code,
                amount_paisa=line.amount_paisa,
                qty_pieces=line.qty_pieces,
                created_by=user,
            )
        self.mark_posted(user=user)
        self.save()
        return self

    @transaction.atomic
    def cancel(self, *, user=None, reason: str = ""):
        self.assert_transition(DocumentStatus.CANCELLED)
        self.assert_cancellable()
        for entry in SampleEntry.objects.filter(document_code=self.code, is_reversal=False):
            SampleEntry.objects.create(
                document_code=self.code,
                amount_paisa=-entry.amount_paisa,
                qty_pieces=-entry.qty_pieces,
                is_reversal=True,
                created_by=user,
            )
        self.mark_cancelled(user=user, reason=reason)
        self.save()
        return self

    @transaction.atomic
    def amend(self, *, user=None):
        """Header via the base helper, then this model's own lines."""
        amendment = self.build_amendment(user=user)
        for line in self.lines.all():
            SampleLine.objects.create(
                document=amendment,
                description=line.description,
                qty_pieces=line.qty_pieces,
                amount_paisa=line.amount_paisa,
            )
        return amendment


class SampleLine(models.Model):
    document = models.ForeignKey(SampleDocument, on_delete=models.CASCADE, related_name="lines")
    description = models.CharField(max_length=100, default="")
    qty_pieces = QuantityField()
    amount_paisa = MoneyField()

    def __str__(self) -> str:
        return f"{self.description} x{self.qty_pieces}"
