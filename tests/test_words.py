"""Amount in words. Snapshotted hard, because a bill is read by a person.

The tables in :mod:`apps.core.words` are hand-written and a typo in one of them
is silent — the number still prints, it just prints wrong, and it prints wrong on
a piece of paper somebody signs. So every boundary that could shift a whole
range is pinned to an exact string here rather than to a property:

* **zero**, and zero rupees with paisa;
* the **teens and the tens**, where English stops composing and starts naming;
* **1,00,000 and 1,00,00,000** — lakh and crore, the two places where the South
  Asian grouping and the Western one part company;
* the digits **either side** of each of those boundaries;
* **paisa**, including the ones that are dropped and the ones that are not.
"""

import pytest

from apps.core.exceptions import MoneyError
from apps.core.money import to_paisa
from apps.core.words import (
    amount_in_words,
    amount_in_words_urdu,
    number_in_words,
    number_in_words_urdu,
)


class TestZeroAndSmall:
    @pytest.mark.parametrize(
        ("paisa", "expected"),
        [
            (0, "Rupees Zero Only"),
            (1, "Rupees Zero and One Paisa Only"),
            (5, "Rupees Zero and Five Paisa Only"),
            (50, "Rupees Zero and Fifty Paisa Only"),
            (99, "Rupees Zero and Ninety Nine Paisa Only"),
            (100, "Rupees One Only"),
            (101, "Rupees One and One Paisa Only"),
        ],
    )
    def test_it_reads_the_way_a_bill_is_written(self, paisa, expected):
        assert amount_in_words(paisa) == expected

    def test_whole_rupees_never_say_zero_paisa(self):
        """ "Rupees Fifty and Zero Paisa Only" is not how anybody writes a cheque."""
        assert amount_in_words(5000) == "Rupees Fifty Only"
        assert "Zero Paisa" not in amount_in_words(5000)


class TestEnglishNumbers:
    @pytest.mark.parametrize(
        ("number", "expected"),
        [
            (0, "Zero"),
            (7, "Seven"),
            (10, "Ten"),
            (11, "Eleven"),
            (15, "Fifteen"),
            (19, "Nineteen"),
            (20, "Twenty"),
            (21, "Twenty One"),
            (40, "Forty"),
            (45, "Forty Five"),
            (99, "Ninety Nine"),
            (100, "One Hundred"),
            (101, "One Hundred One"),
            (110, "One Hundred Ten"),
            (999, "Nine Hundred Ninety Nine"),
        ],
    )
    def test_under_a_thousand(self, number, expected):
        assert number_in_words(number) == expected

    @pytest.mark.parametrize(
        ("number", "expected"),
        [
            (1_000, "One Thousand"),
            (1_001, "One Thousand One"),
            (10_000, "Ten Thousand"),
            (99_999, "Ninety Nine Thousand Nine Hundred Ninety Nine"),
        ],
    )
    def test_thousands(self, number, expected):
        assert number_in_words(number) == expected


class TestLakhAndCroreBoundaries:
    """The whole reason this module exists rather than a library call.

    A Western library says "One Hundred Thousand" for 100000 and "Ten Million"
    for 10000000. Both are wrong on a Pakistani invoice, and both are wrong in a
    way nobody notices until a bank queries a cheque.
    """

    @pytest.mark.parametrize(
        ("number", "expected"),
        [
            # One below the lakh boundary, on it, and one above.
            (99_999, "Ninety Nine Thousand Nine Hundred Ninety Nine"),
            (100_000, "One Lakh"),
            (100_001, "One Lakh One"),
            (101_000, "One Lakh One Thousand"),
            (999_999, "Nine Lakh Ninety Nine Thousand Nine Hundred Ninety Nine"),
            # One below the crore boundary, on it, and one above.
            (9_999_999, "Ninety Nine Lakh Ninety Nine Thousand Nine Hundred Ninety Nine"),
            (10_000_000, "One Crore"),
            (10_000_001, "One Crore One"),
            (10_100_000, "One Crore One Lakh"),
            # And the one after that, which most implementations get wrong.
            (1_000_000_000, "One Arab"),
        ],
    )
    def test_the_grouping_is_south_asian(self, number, expected):
        assert number_in_words(number) == expected

    def test_it_never_says_million_or_billion(self):
        for number in (1_000_000, 10_000_000, 1_000_000_000, 123_456_789):
            words = number_in_words(number)
            assert "Million" not in words, f"{number} -> {words}"
            assert "Billion" not in words, f"{number} -> {words}"

    def test_a_million_is_ten_lakh(self):
        assert number_in_words(1_000_000) == "Ten Lakh"

    def test_a_full_invoice_total(self):
        """Rs 12,34,567.89 — the shape a real bill takes."""
        assert amount_in_words(to_paisa("1234567.89")) == (
            "Rupees Twelve Lakh Thirty Four Thousand Five Hundred Sixty Seven "
            "and Eighty Nine Paisa Only"
        )

    def test_the_largest_figure_this_business_will_ever_print(self):
        assert amount_in_words(to_paisa("999999999.99")) == (
            "Rupees Ninety Nine Crore Ninety Nine Lakh Ninety Nine Thousand Nine Hundred "
            "Ninety Nine and Ninety Nine Paisa Only"
        )


class TestPaisa:
    @pytest.mark.parametrize(
        ("rupees", "expected_tail"),
        [
            ("1.01", "and One Paisa Only"),
            ("1.10", "and Ten Paisa Only"),
            ("1.11", "and Eleven Paisa Only"),
            ("1.19", "and Nineteen Paisa Only"),
            ("1.20", "and Twenty Paisa Only"),
            ("1.99", "and Ninety Nine Paisa Only"),
        ],
    )
    def test_the_paisa_half_is_spelled_out_too(self, rupees, expected_tail):
        assert amount_in_words(to_paisa(rupees)).endswith(expected_tail)

    def test_it_takes_paisa_not_rupees(self):
        """The argument is what the field stores. 100 is one rupee, not a hundred."""
        assert amount_in_words(100) == "Rupees One Only"

    def test_a_float_is_refused(self):
        with pytest.raises(MoneyError):
            amount_in_words(12.5)


class TestNegatives:
    def test_a_credit_note_total_is_prefixed_not_refused(self):
        assert amount_in_words(-5000) == "Minus Rupees Fifty Only"
        assert amount_in_words(-12345) == (
            "Minus Rupees One Hundred Twenty Three and Forty Five Paisa Only"
        )

    def test_the_bare_number_helper_refuses_a_negative(self):
        """The sign is the caller's business — amount_in_words handles it."""
        with pytest.raises(MoneyError):
            number_in_words(-1)


class TestTooLarge:
    def test_it_refuses_rather_than_inventing_a_scale_name(self):
        with pytest.raises(MoneyError, match="typo"):
            number_in_words(10**15)


class TestUrdu:
    """Same grouping, different script — they share ``_indian_groups``."""

    def test_the_table_covers_every_number_below_a_hundred(self):
        from apps.core.words import _URDU_UNDER_100

        assert len(_URDU_UNDER_100) == 100
        assert len(set(_URDU_UNDER_100)) == 100, "a duplicate means a number is spelled wrong"

    @pytest.mark.parametrize(
        ("number", "expected"),
        [
            (0, "صفر"),
            (1, "ایک"),
            (21, "اکیس"),
            (100, "ایک سو"),
            (1_000, "ایک ہزار"),
            (100_000, "ایک لاکھ"),
            (10_000_000, "ایک کروڑ"),
        ],
    )
    def test_the_boundaries_are_the_same_ones(self, number, expected):
        assert number_in_words_urdu(number) == expected

    def test_a_full_amount(self):
        assert amount_in_words_urdu(12345) == "صرف ایک سو تئیس روپے اور پینتالیس پیسے"

    def test_zero_paisa_is_dropped_here_too(self):
        assert amount_in_words_urdu(5000) == "صرف پچاس روپے"

    def test_negatives_are_prefixed(self):
        assert amount_in_words_urdu(-5000).startswith("منفی")


class TestTheTemplateFilter:
    def test_it_renders_through_core_tags(self):
        from django.template import Context, Template

        rendered = Template("{% load core_tags %}{{ v|words }}").render(Context({"v": 123456789}))
        assert rendered == (
            "Rupees Twelve Lakh Thirty Four Thousand Five Hundred Sixty Seven "
            "and Eighty Nine Paisa Only"
        )

    def test_a_blank_stays_blank(self):
        from django.template import Context, Template

        assert Template("{% load core_tags %}{{ v|words }}").render(Context({"v": None})) == ""
