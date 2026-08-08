"""
DocumentModel lifecycle: immutability of posted documents, the append-only
guard, and the amendment chain.

Exercised through tests.testapp.models.SampleDocument, a concrete subclass that
exists only under pytest — see config/settings/test.py.
"""

import pytest
from django.utils import timezone

from apps.core.enums import DocumentStatus
from apps.core.exceptions import AppendOnlyViolation, DocumentImmutable, IllegalTransition
from apps.core.models import DocumentModel
from tests.testapp.models import SampleDocument, SampleEntry, SampleLine

pytestmark = pytest.mark.django_db


@pytest.fixture
def document():
    doc = SampleDocument.objects.create(code="SI-2026-000123", party_name="Ali Traders")
    SampleLine.objects.create(
        document=doc, description="Soap 100g", qty_pieces=12, amount_paisa=60000
    )
    SampleLine.objects.create(document=doc, description="Shampoo", qty_pieces=6, amount_paisa=45000)
    return doc


class TestDraftIsEditable:
    def test_fields_can_be_changed(self, document):
        document.party_name = "Ali Traders & Sons"
        document.save()
        document.refresh_from_db()
        assert document.party_name == "Ali Traders & Sons"

    def test_starts_as_draft_with_no_amendment(self, document):
        assert document.status == DocumentStatus.DRAFT
        assert document.amendment_no == 0
        assert document.amended_from is None
        assert document.is_editable

    def test_a_draft_may_be_deleted(self, document):
        """It has written nothing to any ledger, so nothing is lost."""
        document.delete()
        assert not SampleDocument.objects.filter(code="SI-2026-000123").exists()


class TestPostedIsImmutable:
    def test_posting_writes_entries_and_stamps(self, document, user):
        document.post(user=user)
        document.refresh_from_db()

        assert document.status == DocumentStatus.POSTED
        assert document.posted_at is not None
        assert document.posted_by == user
        assert SampleEntry.objects.filter(document_code=document.code).count() == 2

    def test_changing_a_field_after_posting_raises(self, document, user):
        document.post(user=user)

        document.party_name = "Someone Else"
        with pytest.raises(DocumentImmutable) as exc:
            document.save()

        message = str(exc.value)
        assert "SI-2026-000123" in message, "the error must name the document"
        assert "party_name" in message, "the error must name what was changed"
        assert "POSTED" in message

    def test_immutability_survives_a_reload(self, document, user):
        """The guard reads the loaded row, not in-memory bookkeeping."""
        document.post(user=user)

        reloaded = SampleDocument.objects.get(pk=document.pk)
        reloaded.note = "sneaking a change in"
        with pytest.raises(DocumentImmutable, match="SI-2026-000123"):
            reloaded.save()

    def test_update_fields_does_not_bypass_the_guard(self, document, user):
        document.post(user=user)
        document.party_name = "Someone Else"
        with pytest.raises(DocumentImmutable):
            document.save(update_fields=["party_name"])

    def test_a_posted_document_cannot_be_deleted(self, document, user):
        document.post(user=user)
        with pytest.raises(DocumentImmutable, match="cannot be"):
            document.delete()

    def test_reposting_is_rejected(self, document, user):
        document.post(user=user)
        with pytest.raises(IllegalTransition, match="POSTED -> POSTED"):
            document.post(user=user)

    def test_saving_with_no_changes_is_allowed(self, document, user):
        """A no-op save must not blow up — admin and forms do this constantly."""
        document.post(user=user)
        reloaded = SampleDocument.objects.get(pk=document.pk)
        reloaded.save()


class TestCancellation:
    def test_cancel_writes_reversing_entries_and_leaves_originals(self, document, user):
        document.post(user=user)
        original_ids = set(
            SampleEntry.objects.filter(document_code=document.code).values_list("id", flat=True)
        )

        document.cancel(user=user, reason="Wrong party")
        document.refresh_from_db()

        assert document.status == DocumentStatus.CANCELLED
        assert document.cancelled_at is not None
        assert document.cancelled_by == user
        assert document.cancel_reason == "Wrong party"

        entries = SampleEntry.objects.filter(document_code=document.code)
        assert set(entries.values_list("id", flat=True)) >= original_ids, "originals must survive"
        assert entries.filter(is_reversal=True).count() == 2
        assert sum(entries.values_list("amount_paisa", flat=True)) == 0, "must net to zero"

    def test_a_cancelled_document_is_frozen_completely(self, document, user):
        document.post(user=user)
        document.cancel(user=user, reason="Wrong party")

        document.party_name = "Another Attempt"
        with pytest.raises(DocumentImmutable, match="CANCELLED"):
            document.save()

    def test_cancelling_a_draft_is_rejected(self, document, user):
        with pytest.raises(IllegalTransition, match="DRAFT -> CANCELLED"):
            document.cancel(user=user)

    def test_cancelling_twice_is_rejected(self, document, user):
        document.post(user=user)
        document.cancel(user=user)
        with pytest.raises(IllegalTransition):
            document.cancel(user=user)


class TestAppendOnlyEntries:
    def test_an_entry_cannot_be_updated(self, document, user):
        document.post(user=user)
        entry = SampleEntry.objects.filter(document_code=document.code).first()

        entry.amount_paisa = 1
        with pytest.raises(AppendOnlyViolation, match="reversing row"):
            entry.save()

    def test_an_entry_cannot_be_deleted(self, document, user):
        document.post(user=user)
        entry = SampleEntry.objects.filter(document_code=document.code).first()

        with pytest.raises(AppendOnlyViolation, match="reversing row"):
            entry.delete()


class TestAmendmentChain:
    def _post_and_cancel(self, doc, user):
        doc.post(user=user)
        doc.cancel(user=user, reason="correction")
        return doc

    def test_first_amendment_is_suffixed_1(self, document, user):
        self._post_and_cancel(document, user)

        first = document.amend(user=user)

        assert first.code == "SI-2026-000123-1"
        assert first.status == DocumentStatus.DRAFT
        assert first.amendment_no == 1
        assert first.amended_from == document

    def test_second_amendment_is_suffixed_2_not_1_1(self, document, user):
        """The suffix comes off the root code, so chains do not nest suffixes."""
        self._post_and_cancel(document, user)
        first = self._post_and_cancel(document.amend(user=user), user)

        second = first.amend(user=user)

        assert second.code == "SI-2026-000123-2"
        assert second.amendment_no == 2
        assert second.amended_from == first
        assert second.root_document() == document

    def test_a_long_chain_keeps_counting(self, document, user):
        self._post_and_cancel(document, user)
        current = document
        codes = []
        for _ in range(4):
            current = current.amend(user=user)
            codes.append(current.code)
            self._post_and_cancel(current, user)

        assert codes == [
            "SI-2026-000123-1",
            "SI-2026-000123-2",
            "SI-2026-000123-3",
            "SI-2026-000123-4",
        ]

    def test_amendment_carries_business_fields_but_not_lifecycle_state(self, document, user):
        self._post_and_cancel(document, user)

        amendment = document.amend(user=user)

        assert amendment.party_name == document.party_name
        assert amendment.posted_at is None
        assert amendment.cancelled_at is None
        assert amendment.cancel_reason == ""
        assert amendment.pk != document.pk

    def test_amendment_copies_lines(self, document, user):
        self._post_and_cancel(document, user)

        amendment = document.amend(user=user)

        assert amendment.lines.count() == 2
        assert set(amendment.lines.values_list("amount_paisa", flat=True)) == {60000, 45000}
        assert document.lines.count() == 2, "the original keeps its own lines"

    def test_amending_a_draft_is_rejected(self, document, user):
        with pytest.raises(IllegalTransition, match="only a CANCELLED"):
            document.amend(user=user)

    def test_amending_a_posted_document_is_rejected(self, document, user):
        """Amending before reversal would double-count the original."""
        document.post(user=user)
        with pytest.raises(IllegalTransition, match="only a CANCELLED"):
            document.amend(user=user)

    def test_the_cancelled_original_is_still_reachable(self, document, user):
        self._post_and_cancel(document, user)
        amendment = document.amend(user=user)

        assert document.testapp_sampledocument_amendments.get() == amendment


class TestBaseMethodsRequireImplementation:
    """The base refuses to guess how a document reaches the ledger."""

    @pytest.mark.parametrize("method_name", ["post", "cancel", "amend"])
    def test_base_implementation_raises(self, method_name):
        doc = SampleDocument(code="XX-2026-000001")
        base_method = getattr(DocumentModel, method_name)
        with pytest.raises(NotImplementedError, match=method_name):
            base_method(doc)


class TestTimeStamps:
    def test_created_and_updated_are_recorded(self, document, user):
        assert document.created_at is not None
        assert document.updated_at is not None

        before = timezone.now()
        document.post(user=user)
        document.refresh_from_db()

        assert document.updated_by == user
        assert document.updated_at >= before
