"""Money primitives: parsing, rounding, formatting and the Money value object."""

from decimal import Decimal

import pytest

from apps.core.exceptions import MoneyError
from apps.core.money import (
    Money,
    fmt,
    format_money,
    round_paisa,
    split_evenly,
    to_paisa,
    to_rupees,
)


class TestToPaisaParsing:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("0", 0),
            ("1", 100),
            ("1.5", 150),
            ("1.50", 150),
            ("123.45", 12345),
            ("1,234.50", 123450),
            ("1,234,567.89", 123456789),
            ("  1,234.50  ", 123450),
            ("1 234.50", 123450),  # stray space inside the number
            ("+99.99", 9999),
            (".50", 50),
            ("0.01", 1),
        ],
    )
    def test_parses_strings(self, value, expected):
        assert to_paisa(value) == expected

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("-1", -100),
            ("-0.01", -1),
            ("-123.45", -12345),
            ("-1,234.50", -123450),
            ("-1,234,567.89", -123456789),
        ],
    )
    def test_parses_negatives(self, value, expected):
        assert to_paisa(value) == expected

    def test_accepts_decimal_and_int(self):
        assert to_paisa(Decimal("123.45")) == 12345
        assert to_paisa(Decimal("-0.01")) == -1
        assert to_paisa(5) == 500
        assert to_paisa(0) == 0

    def test_rejects_float(self):
        """Floats are not exact; the caller must pass a str or Decimal."""
        with pytest.raises(MoneyError, match="float"):
            to_paisa(1.5)

    @pytest.mark.parametrize("value", ["", "   ", "abc", "12.3.4", "1,2,3.4.5", "-", ".", "Rs 100"])
    def test_rejects_garbage(self, value):
        with pytest.raises(MoneyError):
            to_paisa(value)

    @pytest.mark.parametrize("value", [None, True, False, [], {}, object()])
    def test_rejects_wrong_types(self, value):
        with pytest.raises(MoneyError):
            to_paisa(value)

    @pytest.mark.parametrize("value", [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")])
    def test_rejects_non_finite_decimals(self, value):
        with pytest.raises(MoneyError):
            to_paisa(value)


class TestBankersRounding:
    """Exact halves go to the nearest EVEN paisa — see round_paisa's docstring."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("0.005", 0),  # 0.5 paisa -> 0 is even
            ("0.015", 2),  # 1.5       -> 2 is even
            ("0.025", 2),  # 2.5       -> 2 is even
            ("0.035", 4),  # 3.5       -> 4 is even
            ("0.045", 4),  # 4.5       -> 4 is even
            ("1.005", 100),  # 100.5   -> 100 is even
            ("1.015", 102),  # 101.5   -> 102 is even
            ("2.675", 268),  # 267.5   -> 268 is even
            ("2.665", 266),  # 266.5   -> 266 is even
        ],
    )
    def test_exact_half_goes_to_even(self, value, expected):
        assert to_paisa(value) == expected

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("-0.005", 0),
            ("-0.015", -2),
            ("-0.025", -2),
            ("-2.675", -268),
        ],
    )
    def test_halves_round_symmetrically_for_negatives(self, value, expected):
        assert to_paisa(value) == expected

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("0.004", 0),
            ("0.006", 1),
            ("0.014", 1),
            ("0.016", 2),
            ("1,234.5678", 123457),
            ("99.999", 10000),
        ],
    )
    def test_non_halves_round_to_nearest(self, value, expected):
        assert to_paisa(value) == expected

    def test_bias_cancels_over_a_run_of_halves(self):
        """The reason for banker's rounding: half-up drifts upward, this does not.

        0.005 .. 0.095 are the paisa values 0.5, 1.5, ... 9.5, which sum to
        exactly 50. Banker's rounding returns 50 — the halves go down and up in
        equal measure. Half-up would return 55, a 10% overstatement on ten
        lines, and that error compounds across a day of invoicing.
        """
        halves = [f"0.0{n}5" for n in range(10)]  # 0.005, 0.015, ... 0.095
        rounded = [to_paisa(v) for v in halves]

        assert rounded == [0, 2, 2, 4, 4, 6, 6, 8, 8, 10]
        assert sum(rounded) == 50  # the exact total; half-up would give 55

    def test_round_paisa_rejects_non_decimal(self):
        with pytest.raises(MoneyError):
            round_paisa(1.5)

    def test_round_paisa_is_the_single_rounding_point(self):
        """Nothing outside money.py may quantize or round a monetary value."""
        from pathlib import Path

        from django.conf import settings

        offenders = []
        for path in Path(settings.BASE_DIR).glob("apps/**/*.py"):
            if path.name in {"money.py"} or "migrations" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            for banned in (".quantize(", "ROUND_HALF", "ROUND_CEILING", "ROUND_DOWN"):
                if banned in text:
                    offenders.append(f"{path}: {banned}")
        assert not offenders, "Rounding must go through money.round_paisa:\n" + "\n".join(offenders)


class TestDisplay:
    @pytest.mark.parametrize(
        ("paisa", "expected"),
        [
            (0, "0.00"),
            (5, "0.05"),
            (50, "0.50"),
            (100, "1.00"),
            (12345, "123.45"),
            (123450, "1,234.50"),
            (123456789, "1,234,567.89"),
            (-123450, "-1,234.50"),
            (-5, "-0.05"),
        ],
    )
    def test_fmt(self, paisa, expected):
        assert fmt(paisa) == expected

    def test_fmt_without_thousands(self):
        assert fmt(123450, thousands=False) == "1234.50"

    def test_format_money_adds_a_symbol(self):
        assert format_money(123450) == "Rs 1,234.50"
        assert format_money(-12345) == "Rs -123.45"

    def test_to_rupees_is_exact(self):
        assert to_rupees(123450) == Decimal("1234.50")
        assert to_rupees(-1) == Decimal("-0.01")

    def test_round_trip_through_rupees_is_lossless(self):
        for paisa in (0, 1, -1, 99, 100, 123456789, -123456789):
            assert to_paisa(to_rupees(paisa)) == paisa

    def test_display_helpers_reject_non_paisa(self):
        with pytest.raises(MoneyError):
            fmt(1.5)
        with pytest.raises(MoneyError):
            to_rupees("100")


class TestSplitEvenly:
    @pytest.mark.parametrize(
        ("total", "parts"),
        [(100, 3), (1, 4), (9999, 7), (-100, 3), (0, 5), (7, 7), (5, 100)],
    )
    def test_never_loses_a_paisa(self, total, parts):
        assert sum(split_evenly(total, parts)) == total

    def test_remainder_goes_to_the_leading_parts(self):
        assert split_evenly(100, 3) == [34, 33, 33]

    def test_rejects_zero_parts(self):
        with pytest.raises(MoneyError):
            split_evenly(100, 0)


class TestMoneyValueObject:
    def test_construction_and_equality(self):
        assert Money(123450) == Money(123450)
        assert Money.zero() == Money(0)
        assert Money.from_rupees("1,234.50") == Money(123450)

    def test_rejects_non_integer_paisa(self):
        for bad in (1.5, "100", Decimal("1.00"), True):
            with pytest.raises(MoneyError):
                Money(bad)

    def test_is_immutable(self):
        amount = Money(100)
        with pytest.raises(AttributeError):
            amount.paisa = 200

    def test_addition_and_subtraction(self):
        assert Money(100) + Money(50) == Money(150)
        assert Money(100) - Money(150) == Money(-50)
        assert -Money(100) == Money(-100)
        assert abs(Money(-100)) == Money(100)

    def test_sum_works_from_zero(self):
        assert sum([Money(100), Money(50), Money(1)]) == Money(151)
        assert sum([], Money.zero()) == Money(0)

    def test_mixing_with_bare_ints_is_rejected(self):
        """The guard that catches 'is this paisa or rupees?' bugs."""
        with pytest.raises(TypeError):
            Money(100) + 100
        with pytest.raises(TypeError):
            Money(100) - 100

    def test_multiplication_by_quantity_is_exact(self):
        assert Money(1999) * 3 == Money(5997)
        assert 3 * Money(1999) == Money(5997)

    def test_multiplication_by_rate_rounds_once(self):
        assert Money(333) * Decimal("0.5") == Money(166)  # 166.5 -> 166 is even
        assert Money(133) * Decimal("0.5") == Money(66)  # 66.5  -> 66 is even

    def test_percent(self):
        assert Money(10000).percent("15") == Money(1500)
        assert Money(10000).percent(Decimal("2.5")) == Money(250)
        assert Money(101).percent("50") == Money(50)  # 50.5 -> 50 is even

    def test_comparison_and_truthiness(self):
        assert Money(1) < Money(2)
        assert Money(-1) < Money(0)
        assert max(Money(1), Money(9)) == Money(9)
        assert bool(Money(1)) is True
        assert bool(Money.zero()) is False

    def test_display(self):
        assert str(Money(123450)) == "1,234.50"
        assert repr(Money(123450)) == "Money(123450)"
        assert Money(123450).rupees == Decimal("1234.50")

    @pytest.mark.parametrize(
        ("total", "weights"),
        [
            (100, [1, 1, 1]),
            (100, [1, 2, 3]),
            (1, [1, 1, 1, 1]),
            (-100, [1, 1, 1]),
            (99999, [7, 11, 13]),
            (0, [1, 2]),
        ],
    )
    def test_allocate_never_loses_a_paisa(self, total, weights):
        parts = Money(total).allocate(weights)
        assert sum(p.paisa for p in parts) == total
        assert len(parts) == len(weights)

    def test_allocate_is_proportional(self):
        assert Money(600).allocate([1, 2, 3]) == [Money(100), Money(200), Money(300)]

    def test_allocate_rejects_bad_weights(self):
        with pytest.raises(MoneyError):
            Money(100).allocate([])
        with pytest.raises(MoneyError):
            Money(100).allocate([0, 0])
        with pytest.raises(MoneyError):
            Money(100).allocate([1, -1])

    def test_split_never_loses_a_paisa(self):
        assert sum(p.paisa for p in Money(100).split(3)) == 100
