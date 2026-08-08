"""
The four stock services: posting, reversal, and the two figures derived from
what they wrote.

Every quantity and every rupee of inventory value this system will ever print
comes out of ``stock_balance`` or ``valuation_rate``, and both are pure
aggregation over rows written by ``post_stock`` and ``reverse_stock``. Nothing
is cached, so these tests are the whole story: if a valuation is right here it
is right in the report and right in the cost of goods sold.

The worked numbers used throughout — 100 @ 10.00, 200 @ 12.50, 300 @ 15.00,
then 250 out — are the same ones in tests/test_valuation.py, so a failure here
that passes there is a posting bug rather than an arithmetic one.
"""

import datetime as dt

import pytest
from django.db import transaction

from apps.accounting.exceptions import (
    AlreadyPosted,
    AlreadyReversed,
    InsufficientStock,
    InvalidPosting,
)
from apps.accounting.models import StockEntry
from apps.accounting.services import (
    post_stock,
    reverse_stock,
    stock_balance,
    valuation_rate,
)
from tests.testapp.models import SampleDocument

pytestmark = pytest.mark.django_db

APRIL = dt.date(2026, 4, 1)
MID_APRIL = dt.date(2026, 4, 15)
MAY = dt.date(2026, 5, 1)
JUNE = dt.date(2026, 6, 1)


# ---------------------------------------------------------------------------
# Helpers shaped like the posting services the purchasing/sales apps will have.
# ---------------------------------------------------------------------------
def document(code: str) -> SampleDocument:
    return SampleDocument.objects.create(code=code, party_name="Ali Traders")


def receive(item, warehouse, qty_base, rate_paisa, *, code, on=APRIL, user=None):
    """A goods receipt: stock in, at what it cost."""
    doc = document(code)
    post_stock(
        doc,
        [
            {
                "item": item,
                "warehouse": warehouse,
                "qty_base": qty_base,
                "rate_paisa": rate_paisa,
            }
        ],
        on,
        user=user,
    )
    return doc


def issue(item, warehouse, qty_base, *, code, on=APRIL, user=None):
    """A delivery: stock out, valued at the average. No rate is supplied."""
    doc = document(code)
    post_stock(
        doc,
        [{"item": item, "warehouse": warehouse, "qty_base": -qty_base}],
        on,
        user=user,
    )
    return doc


# ---------------------------------------------------------------------------
# post_stock — the moving weighted average
# ---------------------------------------------------------------------------
class TestMovingAverage:
    """Three purchases at three different rates, then a sale."""

    @pytest.fixture
    def stocked(self, items, warehouses):
        for index, (qty_base, rate_paisa) in enumerate(
            ((100, 1000), (200, 1250), (300, 1500)), start=1
        ):
            receive(
                items.rice,
                warehouses.main,
                qty_base,
                rate_paisa,
                code=f"GR-2026-00000{index}",
            )
        return items.rice, warehouses.main

    def test_each_receipt_is_valued_at_what_it_cost(self, stocked):
        rows = list(StockEntry.objects.order_by("pk"))

        assert [(r.qty_base, r.rate_paisa, r.value_paisa) for r in rows] == [
            (100, 1000, 100_000),
            (200, 1250, 250_000),
            (300, 1500, 450_000),
        ]

    def test_the_average_after_two_receipts_is_weighted_not_arithmetic(self, items, warehouses):
        """(100 x 1000 + 200 x 1250) / 300 is 1166.66…, not the 1125 you would
        get by averaging the two rates."""
        receive(items.rice, warehouses.main, 100, 1000, code="GR-2026-000001")
        receive(items.rice, warehouses.main, 200, 1250, code="GR-2026-000002")

        assert valuation_rate(items.rice, warehouses.main) == 1167

    def test_the_average_after_three_receipts(self, stocked):
        item, warehouse = stocked

        assert stock_balance(item, warehouse) == (600, 800_000)
        assert valuation_rate(item, warehouse) == 1333

    def test_the_sale_is_valued_at_the_average_at_that_moment(self, stocked):
        item, warehouse = stocked

        issue(item, warehouse, 250, code="SI-2026-000001")

        row = StockEntry.objects.get(qty_base__lt=0)
        assert row.rate_paisa == 1333, "the average, recorded so it never needs recomputing"
        assert row.value_paisa == -333_333, "its exact share of the value held"

    def test_what_is_left_after_the_sale(self, stocked):
        item, warehouse = stocked

        issue(item, warehouse, 250, code="SI-2026-000001")

        assert stock_balance(item, warehouse) == (350, 466_667)
        assert valuation_rate(item, warehouse) == 1333

    def test_a_later_receipt_moves_the_average_again(self, stocked):
        item, warehouse = stocked
        issue(item, warehouse, 250, code="SI-2026-000001")

        receive(item, warehouse, 150, 2000, code="GR-2026-000004")

        # 466667 + 300000 over 500 units.
        assert stock_balance(item, warehouse) == (500, 766_667)
        assert valuation_rate(item, warehouse) == 1533

    def test_selling_everything_empties_the_value_exactly(self, stocked):
        """No stranded paisa. 800000/600 rounds to 1333 and 600 x 1333 is
        799800 — valuing the sweep that way would leave 200 paisa of inventory
        against a quantity of nothing, on the balance sheet, forever."""
        item, warehouse = stocked

        issue(item, warehouse, 600, code="SI-2026-000001")

        assert stock_balance(item, warehouse) == (0, 0)
        assert valuation_rate(item, warehouse) == 1333, "the last rate it was known to be worth"

    def test_the_stored_rate_is_never_recomputed_by_a_later_movement(self, stocked):
        item, warehouse = stocked
        issue(item, warehouse, 250, code="SI-2026-000001")
        before = StockEntry.objects.get(qty_base__lt=0).rate_paisa

        receive(item, warehouse, 500, 9999, code="GR-2026-000004")

        assert StockEntry.objects.get(qty_base__lt=0).rate_paisa == before

    def test_value_always_equals_the_sum_of_the_rows(self, stocked):
        """The invariant behind every stock report: the balance is the rows, and
        there is nothing else it could be."""
        item, warehouse = stocked
        issue(item, warehouse, 250, code="SI-2026-000001")

        rows = StockEntry.objects.all()
        assert stock_balance(item, warehouse) == (
            sum(r.qty_base for r in rows),
            sum(r.value_paisa for r in rows),
        )


class TestValuationIsPerWarehouse:
    def test_two_warehouses_hold_the_same_item_at_different_rates(self, items, warehouses):
        receive(items.rice, warehouses.main, 100, 1000, code="GR-2026-000001")
        receive(items.rice, warehouses.van, 100, 2000, code="GR-2026-000002")

        assert valuation_rate(items.rice, warehouses.main) == 1000
        assert valuation_rate(items.rice, warehouses.van) == 2000

    def test_an_issue_is_valued_at_its_own_warehouses_rate(self, items, warehouses):
        receive(items.rice, warehouses.main, 100, 1000, code="GR-2026-000001")
        receive(items.rice, warehouses.van, 100, 2000, code="GR-2026-000002")

        issue(items.rice, warehouses.van, 10, code="SI-2026-000001")

        assert StockEntry.objects.get(qty_base__lt=0).value_paisa == -20_000

    def test_the_balance_totals_every_warehouse_when_none_is_named(self, items, warehouses):
        receive(items.rice, warehouses.main, 100, 1000, code="GR-2026-000001")
        receive(items.rice, warehouses.van, 100, 2000, code="GR-2026-000002")

        assert stock_balance(items.rice) == (200, 300_000)

    def test_items_do_not_bleed_into_each_other(self, items, warehouses):
        receive(items.rice, warehouses.main, 100, 1000, code="GR-2026-000001")

        assert stock_balance(items.oil, warehouses.main) == (0, 0)
        assert valuation_rate(items.oil, warehouses.main) == 0


class TestSeveralLinesInOneVoucher:
    def test_lines_for_the_same_item_value_against_each_other_in_order(self, items, warehouses):
        """A receipt and an issue of the same item in one voucher must behave
        exactly as they would in two."""
        doc = document("SA-2026-000001")

        post_stock(
            doc,
            [
                {
                    "item": items.rice,
                    "warehouse": warehouses.main,
                    "qty_base": 100,
                    "rate_paisa": 1000,
                },
                {"item": items.rice, "warehouse": warehouses.main, "qty_base": -40},
            ],
            APRIL,
        )

        out = StockEntry.objects.get(qty_base__lt=0)
        assert (out.rate_paisa, out.value_paisa) == (1000, -40_000)
        assert stock_balance(items.rice, warehouses.main) == (60, 60_000)

    def test_a_transfer_between_warehouses_is_two_lines(self, items, warehouses):
        receive(items.rice, warehouses.main, 100, 1000, code="GR-2026-000001")
        doc = document("ST-2026-000001")

        post_stock(
            doc,
            [
                {"item": items.rice, "warehouse": warehouses.main, "qty_base": -30},
                {
                    "item": items.rice,
                    "warehouse": warehouses.van,
                    "qty_base": 30,
                    "rate_paisa": 1000,
                },
            ],
            APRIL,
        )

        assert stock_balance(items.rice, warehouses.main) == (70, 70_000)
        assert stock_balance(items.rice, warehouses.van) == (30, 30_000)
        assert stock_balance(items.rice) == (100, 100_000), "a transfer moves nothing in total"


class TestPostedRows:
    def test_each_row_carries_the_voucher_reference(self, items, warehouses, ledger_voucher):
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
            APRIL,
        )

        row = StockEntry.objects.get()
        assert row.voucher_type == "SampleDocument"
        assert row.voucher_id == ledger_voucher.pk
        assert row.voucher_code == "SI-2026-000001"

    def test_the_posting_date_is_the_one_given_not_today(self, items, warehouses):
        receive(items.rice, warehouses.main, 100, 1000, code="GR-2026-000001", on=JUNE)

        assert StockEntry.objects.get().posting_date == JUNE

    def test_the_author_is_recorded(self, items, warehouses, user):
        receive(items.rice, warehouses.main, 100, 1000, code="GR-2026-000001", user=user)

        assert StockEntry.objects.get().created_by == user

    def test_nothing_is_flagged_as_a_reversal(self, items, warehouses):
        receive(items.rice, warehouses.main, 100, 1000, code="GR-2026-000001")

        assert not StockEntry.objects.filter(is_reversal=True).exists()
        assert not StockEntry.objects.filter(reverses__isnull=False).exists()


class TestPostingIsRejected:
    def test_with_no_lines(self, ledger_voucher):
        with pytest.raises(InvalidPosting, match="no lines"):
            post_stock(ledger_voucher, [], APRIL)

    def test_with_a_zero_quantity(self, items, warehouses, ledger_voucher):
        with pytest.raises(InvalidPosting, match="moves no stock"):
            post_stock(
                ledger_voucher,
                [{"item": items.rice, "warehouse": warehouses.main, "qty_base": 0}],
                APRIL,
            )

    def test_with_a_fractional_quantity(self, items, warehouses, ledger_voucher):
        """CLAUDE.md §2: a pack of 12 is a UOM conversion, not 0.083 of a pack."""
        with pytest.raises(InvalidPosting, match="whole base units"):
            post_stock(
                ledger_voucher,
                [
                    {
                        "item": items.rice,
                        "warehouse": warehouses.main,
                        "qty_base": 2.5,
                        "rate_paisa": 1000,
                    }
                ],
                APRIL,
            )

    def test_with_an_incoming_line_that_has_no_rate(self, items, warehouses, ledger_voucher):
        """Nothing but the document knows what the goods cost."""
        with pytest.raises(InvalidPosting, match="no rate_paisa"):
            post_stock(
                ledger_voucher,
                [{"item": items.rice, "warehouse": warehouses.main, "qty_base": 100}],
                APRIL,
            )

    def test_with_an_outgoing_line_that_supplies_a_rate(self, items, warehouses, ledger_voucher):
        """The rate a sales line knows is the *selling* price. Accepting it would
        value cost of goods sold at the price it sold for and report a gross
        margin of exactly zero on every invoice, forever."""
        with pytest.raises(InvalidPosting, match="selling price"):
            post_stock(
                ledger_voucher,
                [
                    {
                        "item": items.rice,
                        "warehouse": warehouses.main,
                        "qty_base": -10,
                        "rate_paisa": 5000,
                    }
                ],
                APRIL,
            )

    def test_with_a_negative_rate(self, items, warehouses, ledger_voucher):
        with pytest.raises(InvalidPosting, match="never negative"):
            post_stock(
                ledger_voucher,
                [
                    {
                        "item": items.rice,
                        "warehouse": warehouses.main,
                        "qty_base": 100,
                        "rate_paisa": -1000,
                    }
                ],
                APRIL,
            )

    def test_with_a_float_rate(self, items, warehouses, ledger_voucher):
        with pytest.raises(InvalidPosting, match="whole paisa"):
            post_stock(
                ledger_voucher,
                [
                    {
                        "item": items.rice,
                        "warehouse": warehouses.main,
                        "qty_base": 100,
                        "rate_paisa": 10.0,
                    }
                ],
                APRIL,
            )

    def test_with_a_mistyped_key(self, items, warehouses, ledger_voucher):
        """`qty` instead of `qty_base` would otherwise post a silent zero."""
        with pytest.raises(InvalidPosting, match="unknown key"):
            post_stock(
                ledger_voucher,
                [
                    {
                        "item": items.rice,
                        "warehouse": warehouses.main,
                        "qty": 100,
                        "rate_paisa": 1000,
                    }
                ],
                APRIL,
            )

    def test_with_a_missing_item(self, warehouses, ledger_voucher):
        with pytest.raises(InvalidPosting, match="needs an Item"):
            post_stock(
                ledger_voucher,
                [{"warehouse": warehouses.main, "qty_base": 100, "rate_paisa": 1000}],
                APRIL,
            )

    def test_with_a_missing_warehouse(self, items, ledger_voucher):
        with pytest.raises(InvalidPosting, match="needs a Warehouse"):
            post_stock(
                ledger_voucher,
                [{"item": items.rice, "qty_base": 100, "rate_paisa": 1000}],
                APRIL,
            )

    def test_with_a_datetime_posting_date(self, items, warehouses, ledger_voucher):
        from django.utils import timezone

        with pytest.raises(InvalidPosting, match="not a datetime"):
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
                timezone.now(),
            )

    def test_against_an_unsaved_voucher(self, items, warehouses):
        with pytest.raises(InvalidPosting, match="no primary key"):
            post_stock(
                SampleDocument(code="GR-2026-000999"),
                [
                    {
                        "item": items.rice,
                        "warehouse": warehouses.main,
                        "qty_base": 100,
                        "rate_paisa": 1000,
                    }
                ],
                APRIL,
            )

    def test_twice_for_the_same_voucher(self, items, warehouses, ledger_voucher):
        lines = [
            {"item": items.rice, "warehouse": warehouses.main, "qty_base": 100, "rate_paisa": 1000}
        ]
        post_stock(ledger_voucher, lines, APRIL)

        with pytest.raises(AlreadyPosted, match="already has stock entries"):
            post_stock(ledger_voucher, lines, APRIL)

        assert StockEntry.objects.count() == 1, "the second attempt wrote nothing"

    def test_nothing_at_all_is_written_when_one_line_is_bad(
        self, items, warehouses, ledger_voucher
    ):
        with pytest.raises(InvalidPosting):
            post_stock(
                ledger_voucher,
                [
                    {
                        "item": items.rice,
                        "warehouse": warehouses.main,
                        "qty_base": 100,
                        "rate_paisa": 1000,
                    },
                    {"item": items.oil, "warehouse": warehouses.main, "qty_base": 0},
                ],
                APRIL,
            )

        assert StockEntry.objects.count() == 0, "a rejected posting leaves no partial rows"


# ---------------------------------------------------------------------------
# The negative stock guard
# ---------------------------------------------------------------------------
class TestNegativeStockIsBlocked:
    @pytest.fixture
    def stocked(self, items, warehouses):
        receive(items.rice, warehouses.main, 50, 1000, code="GR-2026-000001")
        return items.rice, warehouses.main

    def test_issuing_more_than_is_held_raises(self, stocked):
        item, warehouse = stocked

        with pytest.raises(InsufficientStock):
            issue(item, warehouse, 60, code="SI-2026-000001")

    def test_the_message_names_the_item_and_the_quantity_available(self, stocked):
        item, warehouse = stocked

        with pytest.raises(InsufficientStock) as exc:
            issue(item, warehouse, 60, code="SI-2026-000001")

        message = str(exc.value)
        assert "Basmati Rice 5kg" in message, "the error must name the item"
        assert "50 available" in message

    def test_the_numbers_are_available_as_attributes_for_a_form(self, stocked):
        item, warehouse = stocked

        with pytest.raises(InsufficientStock) as exc:
            issue(item, warehouse, 60, code="SI-2026-000001")

        assert exc.value.item == item
        assert exc.value.warehouse == warehouse
        assert (exc.value.requested, exc.value.available) == (60, 50)

    def test_nothing_is_written(self, stocked):
        item, warehouse = stocked

        with pytest.raises(InsufficientStock):
            issue(item, warehouse, 60, code="SI-2026-000001")

        assert stock_balance(item, warehouse) == (50, 50_000)
        assert StockEntry.objects.count() == 1

    def test_issuing_exactly_what_is_held_is_allowed(self, stocked):
        item, warehouse = stocked

        issue(item, warehouse, 50, code="SI-2026-000001")

        assert stock_balance(item, warehouse) == (0, 0)

    def test_the_guard_counts_earlier_lines_in_the_same_voucher(self, stocked):
        """Two issues of 30 in one voucher is 60, and 60 is not there."""
        item, warehouse = stocked
        doc = document("SI-2026-000001")

        with pytest.raises(InsufficientStock):
            post_stock(
                doc,
                [
                    {"item": item, "warehouse": warehouse, "qty_base": -30},
                    {"item": item, "warehouse": warehouse, "qty_base": -30},
                ],
                APRIL,
            )

    def test_a_receipt_earlier_in_the_voucher_counts_too(self, stocked):
        """...and the same works in the other direction: receive 20 first and
        the 60 is there."""
        item, warehouse = stocked
        doc = document("SA-2026-000001")

        post_stock(
            doc,
            [
                {"item": item, "warehouse": warehouse, "qty_base": 20, "rate_paisa": 1000},
                {"item": item, "warehouse": warehouse, "qty_base": -60},
            ],
            APRIL,
        )

        assert stock_balance(item, warehouse) == (10, 10_000)

    def test_the_guard_is_per_warehouse(self, items, warehouses):
        """Stock in the godown does not cover an issue from the van."""
        receive(items.rice, warehouses.main, 50, 1000, code="GR-2026-000001")

        with pytest.raises(InsufficientStock, match="VAN1"):
            issue(items.rice, warehouses.van, 10, code="SI-2026-000001")

    def test_an_issue_against_nothing_at_all_raises(self, items, warehouses):
        with pytest.raises(InsufficientStock, match="0 available"):
            issue(items.rice, warehouses.main, 1, code="SI-2026-000001")


class TestNegativeStockIsAllowedWhenTheSettingIsOn:
    @pytest.fixture
    def stocked(self, items, warehouses, settings):
        settings.ALLOW_NEGATIVE_STOCK = True
        receive(items.rice, warehouses.main, 50, 1000, code="GR-2026-000001")
        return items.rice, warehouses.main

    def test_the_issue_goes_through(self, stocked):
        item, warehouse = stocked

        issue(item, warehouse, 60, code="SI-2026-000001")

        assert stock_balance(item, warehouse) == (-10, -10_000)

    def test_what_is_there_goes_at_its_real_value_and_the_rest_at_the_current_rate(self, stocked):
        item, warehouse = stocked

        issue(item, warehouse, 60, code="SI-2026-000001")

        row = StockEntry.objects.get(qty_base__lt=0)
        assert (row.rate_paisa, row.value_paisa) == (1000, -60_000)

    def test_an_issue_against_nothing_at_all_uses_the_last_known_rate(self, stocked):
        """The position is empty, so there is no average to take. The last rate
        this item was known to be worth here is the only honest answer."""
        item, warehouse = stocked
        issue(item, warehouse, 50, code="SI-2026-000001")

        issue(item, warehouse, 10, code="SI-2026-000002")

        assert stock_balance(item, warehouse) == (-10, -10_000)
        assert StockEntry.objects.filter(qty_base=-10).get().rate_paisa == 1000

    def test_a_later_receipt_settles_the_deficit(self, stocked):
        item, warehouse = stocked
        issue(item, warehouse, 60, code="SI-2026-000001")

        receive(item, warehouse, 40, 1000, code="GR-2026-000002")

        assert stock_balance(item, warehouse) == (30, 30_000)
        assert valuation_rate(item, warehouse) == 1000

    def test_turning_it_back_off_blocks_the_next_one(self, stocked, settings):
        item, warehouse = stocked
        issue(item, warehouse, 60, code="SI-2026-000001")

        settings.ALLOW_NEGATIVE_STOCK = False

        with pytest.raises(InsufficientStock):
            issue(item, warehouse, 1, code="SI-2026-000002")


# ---------------------------------------------------------------------------
# reverse_stock
# ---------------------------------------------------------------------------
class TestReversal:
    @pytest.fixture
    def sale(self, items, warehouses, user):
        """100 in at 10.00, 50 more at 16.00 — average 12.00 — then 40 out."""
        receive(items.rice, warehouses.main, 100, 1000, code="GR-2026-000001", user=user)
        receive(items.rice, warehouses.main, 50, 1600, code="GR-2026-000002", user=user)
        return issue(items.rice, warehouses.main, 40, code="SI-2026-000001", user=user)

    def test_the_sale_was_valued_at_the_average(self, items, warehouses, sale):
        row = StockEntry.objects.get(qty_base__lt=0)

        assert (row.rate_paisa, row.value_paisa) == (1200, -48_000)
        assert stock_balance(items.rice, warehouses.main) == (110, 132_000)

    def test_reversing_it_restores_both_quantity_and_value(self, items, warehouses, sale):
        reverse_stock(sale)

        assert stock_balance(items.rice, warehouses.main) == (150, 180_000)
        assert valuation_rate(items.rice, warehouses.main) == 1200

    def test_originals_are_not_touched(self, sale):
        """Not updated, not flagged, not re-dated. Byte for byte identical."""
        before = list(StockEntry.objects.filter(is_reversal=False).order_by("pk").values())

        reverse_stock(sale)

        after = list(StockEntry.objects.filter(is_reversal=False).order_by("pk").values())
        assert before == after

    def test_the_mirror_negates_quantity_and_value_but_carries_the_rate(self, sale):
        reverse_stock(sale)

        original = StockEntry.objects.get(is_reversal=False, qty_base__lt=0)
        mirror = StockEntry.objects.get(is_reversal=True)

        assert mirror.qty_base == -original.qty_base
        assert mirror.value_paisa == -original.value_paisa
        assert mirror.rate_paisa == original.rate_paisa, (
            "the rate is a fact about the original, not a fresh valuation"
        )

    def test_the_mirror_is_not_revalued_at_todays_average(self, items, warehouses, sale):
        """This is the failure the carried rate prevents. A receipt at a wildly
        different rate moves the average; the cancellation must still put back
        exactly the 48000 paisa it took out."""
        receive(items.rice, warehouses.main, 100, 9000, code="GR-2026-000003")
        before = stock_balance(items.rice, warehouses.main)

        reverse_stock(sale)

        after = stock_balance(items.rice, warehouses.main)
        assert (after.qty_base - before.qty_base, after.value_paisa - before.value_paisa) == (
            40,
            48_000,
        )

    def test_a_receipt_reversal_takes_the_stock_back_out(self, items, warehouses):
        receipt = receive(items.rice, warehouses.main, 100, 1000, code="GR-2026-000001")

        reverse_stock(receipt)

        assert stock_balance(items.rice, warehouses.main) == (0, 0)

    def test_each_mirror_points_at_what_it_reverses(self, sale):
        original = StockEntry.objects.get(is_reversal=False, qty_base__lt=0)

        reverse_stock(sale)

        mirror = StockEntry.objects.get(reverses=original)
        assert mirror.is_reversal
        assert mirror.item_id == original.item_id
        assert mirror.warehouse_id == original.warehouse_id

    def test_the_voucher_reference_is_carried_over(self, sale):
        reverse_stock(sale)

        mirror = StockEntry.objects.get(is_reversal=True)
        assert mirror.voucher_code == "SI-2026-000001"
        assert mirror.voucher_id == sale.pk

    def test_it_nets_to_zero_on_any_as_of_date(self, items, warehouses, sale):
        """The mirror takes the original's date, so looking back at a moment
        before the cancellation still shows a cancelled document as cancelled."""
        reverse_stock(sale)

        for as_of in (APRIL, MAY, JUNE):
            assert stock_balance(items.rice, warehouses.main, as_of=as_of) == (150, 180_000)

    def test_the_reversal_date_can_be_pushed_to_a_later_period(self, items, warehouses, sale):
        reverse_stock(sale, posting_date=JUNE)

        assert stock_balance(items.rice, warehouses.main, as_of=MAY) == (110, 132_000)
        assert stock_balance(items.rice, warehouses.main, as_of=JUNE) == (150, 180_000)

    def test_the_author_of_the_reversal_is_recorded(self, sale, user):
        reverse_stock(sale, user=user)

        assert StockEntry.objects.get(is_reversal=True).created_by == user

    def test_a_reversal_that_takes_stock_negative_is_not_blocked(self, items, warehouses):
        """Cancelling a goods receipt whose stock has since been sold does
        exactly this. Refusing it would trap the document in POSTED with no
        legal move left — a document must always be cancellable."""
        receipt = receive(items.rice, warehouses.main, 100, 1000, code="GR-2026-000001")
        issue(items.rice, warehouses.main, 80, code="SI-2026-000001")

        reverse_stock(receipt)

        assert stock_balance(items.rice, warehouses.main) == (-80, -80_000)


class TestDoubleReversalIsRefused:
    @pytest.fixture
    def receipt(self, items, warehouses):
        return receive(items.rice, warehouses.main, 100, 1000, code="GR-2026-000001")

    def test_reversing_twice_raises(self, receipt):
        reverse_stock(receipt)

        with pytest.raises(AlreadyReversed, match="already been reversed"):
            reverse_stock(receipt)

    def test_the_balance_stays_at_zero_rather_than_bouncing_back(self, items, warehouses, receipt):
        """Reversing a reversal would put the original movement back and
        silently un-cancel the document."""
        reverse_stock(receipt)
        with pytest.raises(AlreadyReversed):
            reverse_stock(receipt)

        assert stock_balance(items.rice, warehouses.main) == (0, 0)
        assert StockEntry.objects.count() == 2

    def test_reversing_a_voucher_that_was_never_posted_raises(self, ledger_voucher):
        with pytest.raises(AlreadyReversed, match="nothing to reverse"):
            reverse_stock(ledger_voucher)

    def test_re_posting_after_a_reversal_is_still_refused(self, items, warehouses, receipt):
        """A cancelled document is never re-posted; it is replaced by an
        amendment, which is a different voucher."""
        reverse_stock(receipt)

        with pytest.raises(AlreadyPosted):
            post_stock(
                receipt,
                [
                    {
                        "item": items.rice,
                        "warehouse": warehouses.main,
                        "qty_base": 100,
                        "rate_paisa": 1000,
                    }
                ],
                APRIL,
            )

    def test_a_second_voucher_is_unaffected(self, items, warehouses, receipt):
        other = receive(items.rice, warehouses.main, 50, 1000, code="GR-2026-000002")
        reverse_stock(receipt)

        reverse_stock(other)  # must not raise

        assert StockEntry.objects.filter(is_reversal=True).count() == 2


# ---------------------------------------------------------------------------
# Out-of-order posting dates
# ---------------------------------------------------------------------------
class TestOutOfOrderPostingDates:
    """A stock card is read in posting-date order, not in the order it was typed.

    So an entry dated April is valued from what April knew, however long after
    the June rows it was actually entered. What a back-dated entry can never do
    is silently re-value what is already posted — those rows are append-only and
    carry the rate they were written with (CLAUDE.md §3).
    """

    @pytest.fixture
    def out_of_order(self, items, warehouses):
        """Written June, April, May — in that order, dated the other way."""
        receive(items.rice, warehouses.main, 10, 2000, code="GR-2026-000002", on=JUNE)
        receive(items.rice, warehouses.main, 10, 1000, code="GR-2026-000001", on=APRIL)
        issue(items.rice, warehouses.main, 5, code="SI-2026-000001", on=MAY)
        return items.rice, warehouses.main

    def test_the_issue_is_valued_from_the_date_before_it_not_the_rows_before_it(self, out_of_order):
        """The May issue sees only the April receipt. Valuing it against
        everything written so far would have averaged in the June stock — 1500
        instead of 1000 — and charged cost of goods sold for stock that had not
        arrived yet."""
        row = StockEntry.objects.get(qty_base__lt=0)

        assert (row.rate_paisa, row.value_paisa) == (1000, -5_000)

    def test_the_balance_on_each_date_is_right(self, out_of_order):
        item, warehouse = out_of_order

        assert stock_balance(item, warehouse, as_of=dt.date(2026, 3, 31)) == (0, 0)
        assert stock_balance(item, warehouse, as_of=APRIL) == (10, 10_000)
        assert stock_balance(item, warehouse, as_of=MAY) == (5, 5_000)
        assert stock_balance(item, warehouse, as_of=JUNE) == (15, 25_000)
        assert stock_balance(item, warehouse) == (15, 25_000)

    def test_the_valuation_on_each_date_is_right(self, out_of_order):
        item, warehouse = out_of_order

        assert valuation_rate(item, warehouse, as_of=APRIL) == 1000
        assert valuation_rate(item, warehouse, as_of=MAY) == 1000
        assert valuation_rate(item, warehouse, as_of=JUNE) == 1667

    def test_a_back_dated_receipt_before_everything_still_values_from_nothing(
        self, items, warehouses
    ):
        receive(items.rice, warehouses.main, 10, 2000, code="GR-2026-000002", on=JUNE)

        receive(items.rice, warehouses.main, 10, 1000, code="GR-2026-000001", on=APRIL)

        assert stock_balance(items.rice, warehouses.main, as_of=APRIL) == (10, 10_000)
        assert stock_balance(items.rice, warehouses.main) == (20, 30_000)

    def test_two_entries_on_the_same_date_are_valued_in_the_order_they_were_written(
        self, items, warehouses
    ):
        """Same-date ties break by insertion, which is the only order there is."""
        receive(items.rice, warehouses.main, 10, 1000, code="GR-2026-000001", on=APRIL)
        receive(items.rice, warehouses.main, 10, 3000, code="GR-2026-000002", on=APRIL)

        issue(items.rice, warehouses.main, 4, code="SI-2026-000001", on=APRIL)

        row = StockEntry.objects.get(qty_base__lt=0)
        assert (row.rate_paisa, row.value_paisa) == (2000, -8_000)

    def test_a_back_dated_issue_that_would_starve_a_later_date_is_blocked(self, items, warehouses):
        """April has ten in stock, but May already took all ten. Issuing five in
        the middle of April would leave May at minus five — checking only the
        balance on the day itself would wave it through."""
        receive(items.rice, warehouses.main, 10, 1000, code="GR-2026-000001", on=APRIL)
        issue(items.rice, warehouses.main, 10, code="SI-2026-000001", on=MAY)

        with pytest.raises(InsufficientStock, match="0 available"):
            issue(items.rice, warehouses.main, 5, code="SI-2026-000002", on=MID_APRIL)

    def test_a_back_dated_issue_that_nothing_later_needs_is_allowed(self, items, warehouses):
        receive(items.rice, warehouses.main, 10, 1000, code="GR-2026-000001", on=APRIL)
        issue(items.rice, warehouses.main, 4, code="SI-2026-000001", on=MAY)

        issue(items.rice, warehouses.main, 6, code="SI-2026-000002", on=MID_APRIL)

        assert stock_balance(items.rice, warehouses.main) == (0, 0)

    def test_a_datetime_as_of_is_refused(self, items, warehouses):
        from django.utils import timezone

        with pytest.raises(InvalidPosting, match="not a datetime"):
            stock_balance(items.rice, warehouses.main, as_of=timezone.now())


# ---------------------------------------------------------------------------
# The whole cycle
# ---------------------------------------------------------------------------
class TestAcrossPostedCancelledAndAmendedDocuments:
    def test_the_stock_card_stays_right_through_a_full_lifecycle(self, items, warehouses, user):
        """A cancelled document keeps its rows *and* their mirrors, which net to
        zero, so the position is right without anything downstream having to
        know a cancellation ever happened. An amendment is a separate document
        with its own rows, so it simply adds.
        """
        item, warehouse = items.rice, warehouses.main
        receive(item, warehouse, 100, 1000, code="GR-2026-000001", user=user)

        original = document("SI-2026-000123")
        with transaction.atomic():
            post_stock(
                original,
                [{"item": item, "warehouse": warehouse, "qty_base": -30}],
                APRIL,
                user=user,
            )
            original.mark_posted(user=user)
            original.save()
        assert stock_balance(item, warehouse) == (70, 70_000)

        # Cancelled: reversed out, back to where it started.
        with transaction.atomic():
            reverse_stock(original, user=user)
            original.mark_cancelled(user=user, reason="wrong quantity")
            original.save()
        assert stock_balance(item, warehouse) == (100, 100_000)
        assert StockEntry.objects.filter(voucher_id=original.pk).count() == 2, (
            "one row and its mirror — nothing was deleted"
        )

        # Amended: a new document, correcting the quantity upwards.
        amended = original.amend(user=user)
        assert amended.code == "SI-2026-000123-1"
        with transaction.atomic():
            post_stock(
                amended,
                [{"item": item, "warehouse": warehouse, "qty_base": -40}],
                APRIL,
                user=user,
            )
            amended.mark_posted(user=user)
            amended.save()

        assert stock_balance(item, warehouse) == (60, 60_000)
        assert valuation_rate(item, warehouse) == 1000

        # History is not rewritten: the cancelled document's rows are still
        # there, still say what they said, and are still findable by its code.
        cancelled_rows = StockEntry.objects.filter(voucher_code="SI-2026-000123")
        assert cancelled_rows.filter(is_reversal=False).count() == 1
        assert cancelled_rows.filter(is_reversal=True).count() == 1
        assert StockEntry.objects.filter(voucher_code="SI-2026-000123-1").count() == 1
