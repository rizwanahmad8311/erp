"""
Warehouse, and StockEntry the model: the shape of a row, and the fact that a
written row is permanent.

Laid out the same way as tests/test_ledger.py, and for the same reason. Two
layers are tested separately: the Python guards in ``assert_valid`` exist so a
mistake fails with a sentence, and the database CHECK constraints exist so a
mistake fails *at all*, including on the ``bulk_create`` path that never calls
``save()``. Tests that go through ``bulk_create`` here are deliberately reaching
past the friendly layer to prove the hard one is there.
"""

import datetime as dt

import pytest
from django.db import IntegrityError, transaction

from apps.accounting.exceptions import InvalidPosting, InvalidWarehouse
from apps.accounting.models import StockEntry, Warehouse
from apps.accounting.services import post_stock
from apps.core.exceptions import AppendOnlyViolation

pytestmark = pytest.mark.django_db

TODAY = dt.date(2026, 4, 1)


def entry(items, warehouses, **overrides):
    """An unsaved, valid row. Overrides make it invalid one field at a time."""
    fields = {
        "posting_date": TODAY,
        "item": items.rice,
        "warehouse": warehouses.main,
        "qty_base": 100,
        "rate_paisa": 1000,
        "value_paisa": 100_000,
        "voucher_type": "SampleDocument",
        "voucher_id": 1,
        "voucher_code": "GR-2026-000001",
    }
    fields.update(overrides)
    return StockEntry(**fields)


@pytest.fixture
def posted(items, warehouses, ledger_voucher):
    """100 units of rice into the main godown at Rs 10.00 each."""
    post_stock(
        ledger_voucher,
        [
            {
                "item": items.rice,
                "warehouse": warehouses.main,
                "qty_base": 100,
                "rate_paisa": 1000,
            }
        ],
        TODAY,
    )
    return list(StockEntry.objects.order_by("pk"))


# ---------------------------------------------------------------------------
# Warehouse
# ---------------------------------------------------------------------------
class TestWarehouse:
    def test_a_warehouse_saves(self, db):
        Warehouse.objects.create(code="MAIN", name="Main Godown")
        assert Warehouse.objects.count() == 1

    def test_the_code_is_unique(self, warehouses):
        with pytest.raises(IntegrityError), transaction.atomic():
            Warehouse.objects.create(code="MAIN", name="Another Godown")

    def test_a_second_default_is_refused(self, warehouses):
        """Two defaults means "the default warehouse" is whichever row came back
        first, and stock lands somewhere nobody chose."""
        with pytest.raises(InvalidWarehouse, match="already the default"):
            Warehouse.objects.create(code="SHOP", name="Shop Floor", is_default=True)

    def test_the_refusal_names_the_warehouse_that_already_holds_the_flag(self, warehouses):
        with pytest.raises(InvalidWarehouse, match="MAIN"):
            Warehouse.objects.create(code="SHOP", name="Shop Floor", is_default=True)

    def test_promoting_an_existing_warehouse_is_refused_the_same_way(self, warehouses):
        warehouses.van.is_default = True
        with pytest.raises(InvalidWarehouse):
            warehouses.van.save()

    def test_re_saving_the_current_default_is_fine(self, warehouses):
        """The check excludes the row being saved; without that, a rename would
        report the warehouse as clashing with itself."""
        warehouses.main.name = "Main Godown (renamed)"
        warehouses.main.save()

        assert Warehouse.objects.get(code="MAIN").name == "Main Godown (renamed)"

    def test_the_database_refuses_a_second_default_too(self, warehouses):
        """bulk_create skips save(). The partial unique index must still bite."""
        with pytest.raises(IntegrityError), transaction.atomic():
            Warehouse.objects.bulk_create(
                [Warehouse(code="SHOP", name="Shop Floor", is_default=True)]
            )

    def test_many_warehouses_may_be_non_default(self, warehouses):
        """The uniqueness is on True only — the index must not treat every
        False as a duplicate of every other."""
        Warehouse.objects.create(code="SHOP", name="Shop Floor")
        Warehouse.objects.create(code="VAN2", name="Delivery Van 2")

        assert Warehouse.objects.filter(is_default=False).count() == 3

    def test_get_default_returns_it(self, warehouses):
        assert Warehouse.get_default() == warehouses.main

    def test_get_default_raises_rather_than_returning_none(self, db):
        """A caller that reached for the default has no second plan; a silent
        None becomes a stock row with no warehouse a few frames later."""
        with pytest.raises(InvalidWarehouse, match="No warehouse is marked"):
            Warehouse.get_default()

    def test_a_warehouse_with_movement_cannot_be_deleted(self, posted, warehouses):
        from django.db.models import ProtectedError

        with pytest.raises(ProtectedError):
            warehouses.main.delete()


class TestItemProtection:
    def test_an_item_with_movement_cannot_be_deleted(self, posted, items):
        from django.db.models import ProtectedError

        with pytest.raises(ProtectedError):
            items.rice.delete()


# ---------------------------------------------------------------------------
# StockEntry
# ---------------------------------------------------------------------------
class TestRowShape:
    def test_a_valid_row_saves(self, items, warehouses):
        entry(items, warehouses).save()
        assert StockEntry.objects.count() == 1

    def test_an_outgoing_row_saves(self, items, warehouses):
        entry(items, warehouses, qty_base=-100, value_paisa=-100_000).save()
        assert StockEntry.objects.count() == 1

    def test_a_zero_quantity_is_refused(self, items, warehouses):
        with pytest.raises(InvalidPosting, match="qty_base is 0"):
            entry(items, warehouses, qty_base=0, value_paisa=0).save()

    def test_a_fractional_quantity_is_refused(self, items, warehouses):
        """CLAUDE.md §2: there is no half a piece."""
        with pytest.raises(InvalidPosting, match="whole base units"):
            entry(items, warehouses, qty_base=2.5).save()

    def test_a_negative_rate_is_refused(self, items, warehouses):
        with pytest.raises(InvalidPosting, match="never negative"):
            entry(items, warehouses, rate_paisa=-1000).save()

    def test_a_float_rate_is_refused(self, items, warehouses):
        """CLAUDE.md §1: a float never reaches a stored monetary value."""
        with pytest.raises(InvalidPosting, match="whole paisa"):
            entry(items, warehouses, rate_paisa=10.0).save()

    @pytest.mark.parametrize(
        ("qty_base", "value_paisa"),
        [(100, -100_000), (-100, 100_000)],
    )
    def test_quantity_and_value_must_agree_on_direction(
        self, items, warehouses, qty_base, value_paisa
    ):
        with pytest.raises(InvalidPosting, match="disagree on direction"):
            entry(items, warehouses, qty_base=qty_base, value_paisa=value_paisa).save()

    @pytest.mark.parametrize("qty_base", [100, -100])
    def test_a_zero_value_is_allowed_in_both_directions(self, items, warehouses, qty_base):
        """Free goods cost nothing coming in and are issued at nothing."""
        entry(items, warehouses, qty_base=qty_base, rate_paisa=0, value_paisa=0).save()

        assert StockEntry.objects.count() == 1

    def test_a_reversal_flag_without_a_target_is_refused(self, items, warehouses):
        with pytest.raises(InvalidPosting, match="names the row it reverses"):
            entry(items, warehouses, is_reversal=True).save()

    def test_a_target_without_the_reversal_flag_is_refused(self, items, warehouses, posted):
        with pytest.raises(InvalidPosting, match="names the row it reverses"):
            entry(items, warehouses, reverses=posted[0]).save()

    def test_is_inward_reads_the_sign(self, items, warehouses):
        assert entry(items, warehouses, qty_base=100).is_inward
        assert not entry(items, warehouses, qty_base=-100, value_paisa=-100_000).is_inward


class TestDatabaseConstraints:
    """bulk_create skips ``save()``. The database must still say no."""

    def _bulk(self, *objs):
        with pytest.raises(IntegrityError), transaction.atomic():
            StockEntry.objects.bulk_create(list(objs))

    def test_zero_quantity(self, items, warehouses):
        self._bulk(entry(items, warehouses, qty_base=0, value_paisa=0))

    def test_negative_rate(self, items, warehouses):
        self._bulk(entry(items, warehouses, rate_paisa=-1000))

    def test_value_in_while_quantity_goes_out(self, items, warehouses):
        self._bulk(entry(items, warehouses, qty_base=-100, value_paisa=100_000))

    def test_value_out_while_quantity_comes_in(self, items, warehouses):
        self._bulk(entry(items, warehouses, qty_base=100, value_paisa=-100_000))

    def test_reversal_without_a_target(self, items, warehouses):
        self._bulk(entry(items, warehouses, is_reversal=True))

    def test_a_row_can_only_be_reversed_once(self, items, warehouses, posted):
        """The database's own guard against double reversal, independent of the
        check ``reverse_stock`` makes in Python."""
        original = posted[0]
        mirror = entry(
            items,
            warehouses,
            qty_base=-original.qty_base,
            rate_paisa=original.rate_paisa,
            value_paisa=-original.value_paisa,
            is_reversal=True,
            reverses=original,
        )
        mirror.save()

        self._bulk(
            entry(
                items,
                warehouses,
                qty_base=-original.qty_base,
                rate_paisa=original.rate_paisa,
                value_paisa=-original.value_paisa,
                is_reversal=True,
                reverses=original,
            )
        )

    def test_two_different_rows_may_each_be_reversed(self, items, warehouses, ledger_voucher):
        """The uniqueness is per reversed row, not global — the partial index
        must not treat NULLs or siblings as duplicates."""
        post_stock(
            ledger_voucher,
            [
                {
                    "item": items.rice,
                    "warehouse": warehouses.main,
                    "qty_base": 10,
                    "rate_paisa": 1000,
                },
                {
                    "item": items.oil,
                    "warehouse": warehouses.main,
                    "qty_base": 20,
                    "rate_paisa": 1500,
                },
            ],
            TODAY,
        )

        for original in StockEntry.objects.order_by("pk"):
            entry(
                items,
                warehouses,
                item=original.item,
                qty_base=-original.qty_base,
                rate_paisa=original.rate_paisa,
                value_paisa=-original.value_paisa,
                is_reversal=True,
                reverses=original,
            ).save()

        assert StockEntry.objects.filter(is_reversal=True).count() == 2


class TestAppendOnly:
    def test_a_row_cannot_be_deleted(self, posted):
        with pytest.raises(AppendOnlyViolation, match="cannot be deleted"):
            posted[0].delete()

        assert StockEntry.objects.count() == 1

    def test_the_delete_error_names_the_voucher(self, posted):
        with pytest.raises(AppendOnlyViolation) as exc:
            posted[0].delete()
        assert "SI-2026-000001" in str(exc.value)

    def test_a_row_cannot_be_updated(self, posted):
        row = posted[0]
        row.qty_base = 1

        with pytest.raises(AppendOnlyViolation, match="append-only"):
            row.save()

        row.refresh_from_db()
        assert row.qty_base == 100

    def test_a_row_reloaded_from_the_database_cannot_be_updated(self, posted):
        """The guard is on the pk, so it survives a fresh instance."""
        reloaded = StockEntry.objects.get(pk=posted[0].pk)
        reloaded.rate_paisa = 1

        with pytest.raises(AppendOnlyViolation):
            reloaded.save()

    def test_update_fields_does_not_get_past_it(self, posted):
        posted[0].rate_paisa = 1
        with pytest.raises(AppendOnlyViolation):
            posted[0].save(update_fields=["rate_paisa"])

    def test_an_untouched_save_is_still_refused(self, posted):
        """Unlike a document, a stock row has no legitimate second save at all —
        not even a no-op one."""
        with pytest.raises(AppendOnlyViolation):
            posted[0].save()

    def test_an_invalid_edit_still_reports_the_real_reason(self, posted):
        """Reporting a bad quantity would invite a second attempt with a better
        one. The row is permanent; that is what has to be said."""
        row = posted[0]
        row.qty_base = 0

        with pytest.raises(AppendOnlyViolation):
            row.save()

    def test_queryset_update_is_refused(self, posted):
        """The bulk route is the one people take by accident, in a shell or a
        data migration, and it never loads an instance."""
        with pytest.raises(AppendOnlyViolation, match=r"QuerySet\.update"):
            StockEntry.objects.filter(pk=posted[0].pk).update(qty_base=1)

        assert StockEntry.objects.get(pk=posted[0].pk).qty_base == 100

    def test_queryset_delete_is_refused(self, posted):
        with pytest.raises(AppendOnlyViolation, match=r"QuerySet\.delete"):
            StockEntry.objects.all().delete()

        assert StockEntry.objects.count() == 1

    def test_related_manager_update_is_refused(self, items, posted):
        with pytest.raises(AppendOnlyViolation):
            items.rice.stock_entries.update(rate_paisa=1)

    def test_bulk_update_is_refused(self, posted):
        posted[0].rate_paisa = 1
        with pytest.raises(AppendOnlyViolation, match="bulk_update"):
            StockEntry.objects.bulk_update(posted, ["rate_paisa"])

    def test_bulk_create_with_update_conflicts_is_refused(self, items, warehouses):
        with pytest.raises(AppendOnlyViolation, match="update_conflicts"):
            StockEntry.objects.bulk_create(
                [entry(items, warehouses)],
                update_conflicts=True,
                update_fields=["rate_paisa"],
                unique_fields=["id"],
            )

    def test_bulk_create_itself_still_works(self, items, warehouses):
        """It is how post_stock writes; the guard must not block inserts."""
        created = StockEntry.objects.bulk_create(
            [entry(items, warehouses), entry(items, warehouses, item=items.oil)]
        )
        assert len(created) == 2
        assert StockEntry.objects.count() == 2
