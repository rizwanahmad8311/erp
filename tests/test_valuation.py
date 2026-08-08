"""
The moving weighted average, as arithmetic.

No database here on purpose. This is the part that has to be right, and every
awkward case — the rate that does not divide evenly, the sweep that must land on
exactly zero, the position with nothing in it — is much easier to pin down
without a voucher and a transaction wrapped around it.

The two properties everything else rests on:

* issuing the whole position returns the whole value, to the paisa;
* value is what adds up, and where a rounded rate disagrees with it, value wins.
"""

import pytest

from apps.accounting.exceptions import InvalidPosting
from apps.accounting.valuation import Movement, Position


class TestRate:
    def test_an_empty_position_has_no_rate(self):
        assert Position().rate_paisa == 0

    def test_it_is_value_over_quantity(self):
        assert Position(qty_base=100, value_paisa=100_000).rate_paisa == 1000

    def test_it_rounds_once_through_round_paisa(self):
        """350000/300 is 1166.66…; banker's rounding takes it up to 1167."""
        assert Position(qty_base=300, value_paisa=350_000).rate_paisa == 1167

    def test_an_oversold_position_has_no_rate_to_offer(self):
        assert Position(qty_base=-5, value_paisa=-5000).rate_paisa == 0

    def test_an_under_water_position_has_no_rate_to_offer(self):
        """Positive quantity, negative value. Only reachable under
        ALLOW_NEGATIVE_STOCK, and a negative rate is not an answer."""
        assert Position(qty_base=10, value_paisa=-5000).rate_paisa == 0


class TestReceive:
    def test_value_is_quantity_times_rate_exactly(self):
        assert Position().receive(100, 1000) == Movement(100, 1000, 100_000)

    def test_the_given_rate_is_stored_not_the_average(self):
        """Incoming is valued at what it cost. The average is an output of that,
        never an input to it."""
        movement = Position(qty_base=100, value_paisa=100_000).receive(50, 1600)

        assert movement.rate_paisa == 1600
        assert movement.value_paisa == 80_000

    def test_free_goods_are_allowed_and_drag_the_average_down(self):
        position = Position(qty_base=100, value_paisa=100_000).apply(Position().receive(100, 0))

        assert position == Position(qty_base=200, value_paisa=100_000)
        assert position.rate_paisa == 500

    def test_a_negative_rate_is_refused(self):
        with pytest.raises(InvalidPosting, match="never negative"):
            Position().receive(10, -100)

    def test_a_fractional_quantity_is_refused(self):
        with pytest.raises(InvalidPosting, match="whole base units"):
            Position().receive(2.5, 1000)

    def test_a_non_positive_quantity_is_refused(self):
        with pytest.raises(InvalidPosting, match="positive magnitude"):
            Position().receive(0, 1000)


class TestIssue:
    def test_it_is_valued_at_the_average_and_signed_negative(self):
        movement = Position(qty_base=150, value_paisa=180_000).issue(40)

        assert movement == Movement(qty_base=-40, rate_paisa=1200, value_paisa=-48_000)

    def test_a_full_sweep_lands_on_exactly_zero(self):
        """The reason issue is not `qty * rate`. 800000/600 rounds to 1333, and
        600 x 1333 is 799800 — which would strand 200 paisa of inventory value
        against a quantity of nothing."""
        position = Position(qty_base=600, value_paisa=800_000)

        movement = position.issue(600)

        assert movement.value_paisa == -800_000
        assert position.apply(movement) == Position(0, 0)

    def test_a_partial_issue_takes_its_share_not_its_rate(self):
        position = Position(qty_base=600, value_paisa=800_000)

        movement = position.issue(250)

        assert movement.rate_paisa == 1333, "the average, rounded, recorded for audit"
        assert movement.value_paisa == -333_333, "the exact share, which is what adds up"
        assert position.apply(movement) == Position(qty_base=350, value_paisa=466_667)

    def test_an_empty_position_falls_back_to_the_rate_it_is_given(self):
        movement = Position().issue(10, fallback_rate_paisa=1500)

        assert movement == Movement(qty_base=-10, rate_paisa=1500, value_paisa=-15_000)

    def test_an_empty_position_with_no_fallback_issues_at_nothing(self):
        assert Position().issue(10) == Movement(-10, 0, 0)

    def test_an_overdraft_takes_what_is_there_then_the_current_rate(self):
        """50 units really cost 50000; the 10 that are not there go at the rate
        that was current when they were taken."""
        movement = Position(qty_base=50, value_paisa=50_000).issue(60)

        assert movement == Movement(qty_base=-60, rate_paisa=1000, value_paisa=-60_000)

    def test_an_under_water_position_issues_at_zero_rather_than_handing_value_back(self):
        """Quantity and value must never move in opposite directions — that is a
        database CHECK. The deficit stays visible in the balance instead."""
        movement = Position(qty_base=10, value_paisa=-5000).issue(4)

        assert movement == Movement(qty_base=-4, rate_paisa=0, value_paisa=0)

    def test_a_fractional_quantity_is_refused(self):
        with pytest.raises(InvalidPosting, match="whole base units"):
            Position(qty_base=100, value_paisa=100_000).issue(2.5)


class TestRunningAverage:
    def test_three_receipts_at_different_rates_then_an_issue(self):
        """The worked example, as pure arithmetic. The same numbers are posted
        through the service in tests/test_stock_services.py."""
        position = Position()
        for qty_base, rate_paisa in ((100, 1000), (200, 1250), (300, 1500)):
            position = position.apply(position.receive(qty_base, rate_paisa))

        assert position == Position(qty_base=600, value_paisa=800_000)
        assert position.rate_paisa == 1333

        position = position.apply(position.issue(250))

        assert position == Position(qty_base=350, value_paisa=466_667)
        assert position.rate_paisa == 1333

    def test_the_order_of_receipts_does_not_change_the_total(self):
        """Weighted average is order-independent while nothing is issued — which
        is what makes a back-dated *receipt* harmless."""
        forwards = Position()
        for qty_base, rate_paisa in ((100, 1000), (200, 1250), (300, 1500)):
            forwards = forwards.apply(forwards.receive(qty_base, rate_paisa))

        backwards = Position()
        for qty_base, rate_paisa in ((300, 1500), (200, 1250), (100, 1000)):
            backwards = backwards.apply(backwards.receive(qty_base, rate_paisa))

        assert forwards == backwards


class TestPositionIsImmutable:
    def test_applying_a_movement_returns_a_new_position(self):
        original = Position(qty_base=100, value_paisa=100_000)

        original.apply(original.issue(10))

        assert original == Position(qty_base=100, value_paisa=100_000)

    def test_it_cannot_be_written_to(self):
        with pytest.raises(AttributeError):
            Position().qty_base = 5
