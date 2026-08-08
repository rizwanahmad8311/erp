"""
LedgerEntry the model: the shape of a row, and the fact that a written row is
permanent.

Two layers are tested separately on purpose. The Python guards in
``assert_valid`` exist so a mistake fails with a sentence; the database CHECK
constraints exist so a mistake fails *at all*, including on the ``bulk_create``
path that never calls ``save()``. Tests that go through ``bulk_create`` here are
deliberately reaching past the friendly layer to prove the hard one is there.
"""

import datetime as dt

import pytest
from django.db import IntegrityError, transaction

from apps.accounting.enums import PartyType
from apps.accounting.exceptions import GroupAccountPosting, InvalidPosting
from apps.accounting.models import LedgerEntry
from apps.accounting.services import post_entries
from apps.core.exceptions import AppendOnlyViolation

pytestmark = pytest.mark.django_db

TODAY = dt.date(2026, 4, 1)


def entry(accounts, **overrides):
    """An unsaved, valid row. Overrides make it invalid one field at a time."""
    fields = {
        "posting_date": TODAY,
        "account": accounts.cash,
        "debit_paisa": 50000,
        "credit_paisa": 0,
        "voucher_type": "SampleDocument",
        "voucher_id": 1,
        "voucher_code": "SI-2026-000001",
    }
    fields.update(overrides)
    return LedgerEntry(**fields)


@pytest.fixture
def posted(accounts, ledger_voucher):
    """Two rows on the books: Rs 500 out of the bank, into rent."""
    post_entries(
        ledger_voucher,
        [
            {"account": accounts.rent, "debit_paisa": 50000, "remarks": "April rent"},
            {"account": accounts.bank, "credit_paisa": 50000},
        ],
        TODAY,
    )
    return list(LedgerEntry.objects.order_by("pk"))


class TestRowShape:
    def test_a_valid_row_saves(self, accounts):
        entry(accounts).save()
        assert LedgerEntry.objects.count() == 1

    def test_both_sides_set_is_refused(self, accounts):
        with pytest.raises(InvalidPosting, match="exactly one non-zero debit or credit"):
            entry(accounts, debit_paisa=50000, credit_paisa=50000).save()

    def test_neither_side_set_is_refused(self, accounts):
        with pytest.raises(InvalidPosting, match="exactly one non-zero debit or credit"):
            entry(accounts, debit_paisa=0, credit_paisa=0).save()

    @pytest.mark.parametrize("side", ["debit_paisa", "credit_paisa"])
    def test_a_negative_amount_is_refused(self, accounts, side):
        with pytest.raises(InvalidPosting, match="never negative"):
            entry(accounts, **{side: -50000}).save()

    def test_a_float_amount_is_refused(self, accounts):
        """CLAUDE.md §1: a float never reaches a stored monetary value."""
        with pytest.raises(InvalidPosting, match="whole paisa"):
            entry(accounts, debit_paisa=500.0).save()

    def test_a_group_account_is_refused(self, accounts):
        with pytest.raises(GroupAccountPosting, match="is a group"):
            entry(accounts, account=accounts.expenses_group).save()

    def test_a_half_set_party_is_refused(self, accounts):
        with pytest.raises(InvalidPosting, match="together or not at all"):
            entry(accounts, party_type=PartyType.CLIENT).save()

        with pytest.raises(InvalidPosting, match="together or not at all"):
            entry(accounts, party_id=7).save()

    def test_a_full_party_is_accepted(self, accounts):
        entry(accounts, party_type=PartyType.CLIENT, party_id=7).save()
        assert LedgerEntry.objects.filter(party_type="CLIENT", party_id=7).count() == 1

    def test_a_reversal_flag_without_a_target_is_refused(self, accounts):
        with pytest.raises(InvalidPosting, match="names the row it reverses"):
            entry(accounts, is_reversal=True).save()

    def test_a_target_without_the_reversal_flag_is_refused(self, accounts, posted):
        with pytest.raises(InvalidPosting, match="names the row it reverses"):
            entry(accounts, reverses=posted[0]).save()

    def test_signed_paisa_reads_the_row_without_re_signing_it(self, accounts):
        assert entry(accounts, debit_paisa=50000).signed_paisa == 50000
        assert entry(accounts, debit_paisa=0, credit_paisa=50000).signed_paisa == -50000


class TestDatabaseConstraints:
    """bulk_create skips ``save()``. The database must still say no."""

    def _bulk(self, *objs):
        with pytest.raises(IntegrityError), transaction.atomic():
            LedgerEntry.objects.bulk_create(list(objs))

    def test_both_sides_set(self, accounts):
        self._bulk(entry(accounts, debit_paisa=50000, credit_paisa=50000))

    def test_neither_side_set(self, accounts):
        self._bulk(entry(accounts, debit_paisa=0, credit_paisa=0))

    def test_negative_debit(self, accounts):
        self._bulk(entry(accounts, debit_paisa=-50000))

    def test_negative_credit(self, accounts):
        self._bulk(entry(accounts, debit_paisa=0, credit_paisa=-50000))

    def test_half_set_party(self, accounts):
        self._bulk(entry(accounts, party_id=7))

    def test_reversal_without_a_target(self, accounts):
        self._bulk(entry(accounts, is_reversal=True))

    def test_a_row_can_only_be_reversed_once(self, accounts, posted):
        """The database's own guard against double reversal, independent of the
        check ``reverse_entries`` makes in Python."""
        original = posted[0]
        mirror = entry(
            accounts,
            account=original.account,
            debit_paisa=original.credit_paisa,
            credit_paisa=original.debit_paisa,
            is_reversal=True,
            reverses=original,
        )
        mirror.save()

        self._bulk(
            entry(
                accounts,
                account=original.account,
                debit_paisa=original.credit_paisa,
                credit_paisa=original.debit_paisa,
                is_reversal=True,
                reverses=original,
            )
        )

    def test_two_different_rows_may_each_be_reversed(self, accounts, posted):
        """The uniqueness is per reversed row, not global — the partial index
        must not treat NULLs or siblings as duplicates."""
        for original in posted:
            entry(
                accounts,
                account=original.account,
                debit_paisa=original.credit_paisa,
                credit_paisa=original.debit_paisa,
                is_reversal=True,
                reverses=original,
            ).save()

        assert LedgerEntry.objects.filter(is_reversal=True).count() == 2


class TestAppendOnly:
    def test_a_row_cannot_be_deleted(self, posted):
        with pytest.raises(AppendOnlyViolation, match="cannot be deleted"):
            posted[0].delete()

        assert LedgerEntry.objects.count() == 2

    def test_the_delete_error_names_the_voucher(self, posted):
        with pytest.raises(AppendOnlyViolation) as exc:
            posted[0].delete()
        assert "SI-2026-000001" in str(exc.value)

    def test_a_row_cannot_be_updated(self, posted):
        row = posted[0]
        row.debit_paisa = 1

        with pytest.raises(AppendOnlyViolation, match="append-only"):
            row.save()

        row.refresh_from_db()
        assert row.debit_paisa == 50000

    def test_a_row_reloaded_from_the_database_cannot_be_updated(self, posted):
        """The guard is on the pk, so it survives a fresh instance."""
        reloaded = LedgerEntry.objects.get(pk=posted[0].pk)
        reloaded.remarks = "sneaking a change in"

        with pytest.raises(AppendOnlyViolation):
            reloaded.save()

    def test_update_fields_does_not_get_past_it(self, posted):
        posted[0].remarks = "nope"
        with pytest.raises(AppendOnlyViolation):
            posted[0].save(update_fields=["remarks"])

    def test_an_invalid_edit_still_reports_the_real_reason(self, posted):
        """Reporting bad amounts would invite a second attempt with better
        amounts. The row is permanent; that is what has to be said."""
        row = posted[0]
        row.debit_paisa = -1

        with pytest.raises(AppendOnlyViolation):
            row.save()

    def test_an_untouched_save_is_still_refused(self, posted):
        """Unlike a document, a ledger row has no legitimate second save at
        all — not even a no-op one."""
        with pytest.raises(AppendOnlyViolation):
            posted[0].save()

    def test_queryset_update_is_refused(self, posted):
        """The bulk route is the one people take by accident, in a shell or a
        data migration, and it never loads an instance."""
        with pytest.raises(AppendOnlyViolation, match=r"QuerySet\.update"):
            LedgerEntry.objects.filter(pk=posted[0].pk).update(debit_paisa=1)

        assert LedgerEntry.objects.get(pk=posted[0].pk).debit_paisa == 50000

    def test_queryset_delete_is_refused(self, posted):
        with pytest.raises(AppendOnlyViolation, match=r"QuerySet\.delete"):
            LedgerEntry.objects.all().delete()

        assert LedgerEntry.objects.count() == 2

    def test_related_manager_update_is_refused(self, accounts, posted):
        with pytest.raises(AppendOnlyViolation):
            accounts.rent.entries.update(remarks="nope")

    def test_bulk_update_is_refused(self, posted):
        posted[0].remarks = "nope"
        with pytest.raises(AppendOnlyViolation, match="bulk_update"):
            LedgerEntry.objects.bulk_update(posted, ["remarks"])

    def test_bulk_create_with_update_conflicts_is_refused(self, accounts):
        with pytest.raises(AppendOnlyViolation, match="update_conflicts"):
            LedgerEntry.objects.bulk_create(
                [entry(accounts)],
                update_conflicts=True,
                update_fields=["remarks"],
                unique_fields=["id"],
            )

    def test_bulk_create_itself_still_works(self, accounts):
        """It is how post_entries writes; the guard must not block inserts."""
        created = LedgerEntry.objects.bulk_create(
            [entry(accounts), entry(accounts, debit_paisa=0, credit_paisa=50000)]
        )
        assert len(created) == 2
        assert LedgerEntry.objects.count() == 2
