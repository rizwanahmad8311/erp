"""Money written out in words, the way a Pakistani invoice writes it.

    amount_in_words(12345678)  ->  "Rupees One Lakh Twenty Three Thousand Four
                                    Hundred Fifty Six and Seventy Eight Paisa Only"

**Display only**, like :func:`~apps.core.money.fmt` next door. Nothing here is
ever parsed back and nothing here rounds — the input is already whole paisa.

The South Asian numbering system
--------------------------------
This is the whole reason the function exists rather than a one-line call to some
library. After a thousand, the grouping stops being three digits at a time:

    1,000            One Thousand
    1,00,000         One Lakh              (10^5 — not "hundred thousand")
    1,00,00,000      One Crore             (10^7 — not "ten million")
    1,00,00,00,000   One Arab              (10^9)

A cheque written "Ten Million" in a Karachi bank is a cheque that gets queried,
and an invoice that says "Rupees One Million Two Hundred Thousand" reads as a
translation error to the person paying it. The groupings are 2-2-2-3 from the
right, not 3-3-3, and that is what :func:`_indian_groups` does.

Urdu
----
:func:`amount_in_words_urdu` writes the same amount in Urdu script, for the
businesses that print a bilingual bill. It uses the identical grouping — لاکھ
and کروڑ are the same 10^5 and 10^7 — so the two functions can never disagree
about where the boundaries are: they share :func:`_indian_groups`.
"""

from __future__ import annotations

from .exceptions import MoneyError

PAISA_PER_RUPEE = 100

# ---------------------------------------------------------------------------
# English
# ---------------------------------------------------------------------------
_ONES = (
    "Zero",
    "One",
    "Two",
    "Three",
    "Four",
    "Five",
    "Six",
    "Seven",
    "Eight",
    "Nine",
    "Ten",
    "Eleven",
    "Twelve",
    "Thirteen",
    "Fourteen",
    "Fifteen",
    "Sixteen",
    "Seventeen",
    "Eighteen",
    "Nineteen",
)
_TENS = (
    "",
    "",
    "Twenty",
    "Thirty",
    "Forty",
    "Fifty",
    "Sixty",
    "Seventy",
    "Eighty",
    "Ninety",
)

#: Group names from the right, after the leading 0-999. One entry per two
#: decimal digits, which is exactly what makes this the South Asian system.
_SCALES = ("", "Thousand", "Lakh", "Crore", "Arab", "Kharab")

# ---------------------------------------------------------------------------
# Urdu
# ---------------------------------------------------------------------------
# 0-99 spelled out rather than composed: an Urdu numeral below a hundred is not
# a tens word plus a ones word the way English builds "twenty three" — each has
# its own name, and generating them would be wrong for most of them.
#
# Laid out one decade per row, and the length is asserted below. Right-to-left
# text in a left-to-right source file is genuinely hard to proofread, and a
# table that is one entry short would silently shift every number above the gap.
_URDU_DECADES = (
    ("صفر", "ایک", "دو", "تین", "چار", "پانچ", "چھ", "سات", "آٹھ", "نو"),
    ("دس", "گیارہ", "بارہ", "تیرہ", "چودہ", "پندرہ", "سولہ", "سترہ", "اٹھارہ", "انیس"),
    ("بیس", "اکیس", "بائیس", "تئیس", "چوبیس", "پچیس", "چھبیس", "ستائیس", "اٹھائیس", "انتیس"),
    ("تیس", "اکتیس", "بتیس", "تینتیس", "چونتیس", "پینتیس", "چھتیس", "سینتیس", "اڑتیس", "انتالیس"),
    ("چالیس", "اکتالیس", "بیالیس", "تینتالیس", "چوالیس", "پینتالیس", "چھیالیس", "سینتالیس", "اڑتالیس", "انچاس"),
    ("پچاس", "اکاون", "باون", "ترپن", "چون", "پچپن", "چھپن", "ستاون", "اٹھاون", "انسٹھ"),
    ("ساٹھ", "اکسٹھ", "باسٹھ", "تریسٹھ", "چونسٹھ", "پینسٹھ", "چھیاسٹھ", "سڑسٹھ", "اڑسٹھ", "انہتر"),
    ("ستر", "اکہتر", "بہتر", "تہتر", "چوہتر", "پچہتر", "چھہتر", "ستتر", "اٹھہتر", "اناسی"),
    ("اسی", "اکیاسی", "بیاسی", "تراسی", "چوراسی", "پچاسی", "چھیاسی", "ستاسی", "اٹھاسی", "نواسی"),
    ("نوے", "اکانوے", "بانوے", "ترانوے", "چورانوے", "پچانوے", "چھیانوے", "ستانوے", "اٹھانوے", "ننانوے"),
)  # fmt: skip

_URDU_UNDER_100 = tuple(word for decade in _URDU_DECADES for word in decade)

if len(_URDU_UNDER_100) != 100:  # pragma: no cover - a table typo, caught at import
    raise AssertionError(
        f"The Urdu number table has {len(_URDU_UNDER_100)} entries, not 100. "
        f"Every number above the gap would be spelled wrong."
    )

_URDU_HUNDRED = "سو"
_URDU_SCALES = ("", "ہزار", "لاکھ", "کروڑ", "ارب", "کھرب")
_URDU_RUPEES = "روپے"
_URDU_PAISA = "پیسے"
_URDU_AND = "اور"
_URDU_ONLY = "صرف"


def _as_paisa_int(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MoneyError(
            f"amount_in_words expects whole paisa as an int, got {type(value).__name__}: "
            f"{value!r}. Display helpers take what the field stores."
        )
    return value


def _indian_groups(number: int) -> list[int]:
    """Split into South Asian groups, **least significant first**.

        1234567 -> [567, 34, 12]        (567 + 34 thousand + 12 lakh)

    Three digits for the first group, then two at a time. That single asymmetry
    is the whole difference from the Western system, and both language functions
    read it from here so they cannot drift apart.
    """
    if number == 0:
        return [0]

    groups = [number % 1000]
    number //= 1000
    while number:
        groups.append(number % 100)
        number //= 100
    return groups


def _english_under_1000(number: int) -> str:
    """1-999 in words. Never called with 0 — an empty group is skipped."""
    hundreds, rest = divmod(number, 100)
    parts = []
    if hundreds:
        parts.append(f"{_ONES[hundreds]} Hundred")
    if rest:
        if rest < 20:
            parts.append(_ONES[rest])
        else:
            tens, ones = divmod(rest, 10)
            parts.append(_TENS[tens] if not ones else f"{_TENS[tens]} {_ONES[ones]}")
    return " ".join(parts)


def number_in_words(number: int) -> str:
    """A whole non-negative number in English words, South Asian grouping.

        number_in_words(0)         -> "Zero"
        number_in_words(100000)    -> "One Lakh"
        number_in_words(10000000)  -> "One Crore"

    Raises above ``10^14`` rather than inventing a scale name: a figure that
    large in a distribution business is a data-entry accident, and printing
    "Ninety Nine Thousand Kharab" on an invoice hides it.
    """
    number = _as_paisa_int(number)
    if number < 0:
        raise MoneyError("number_in_words takes a non-negative number; handle the sign outside.")
    if number == 0:
        return _ONES[0]

    groups = _indian_groups(number)
    if len(groups) > len(_SCALES):
        raise MoneyError(
            f"{number} is larger than this system spells out (max {10**14 - 1}). "
            f"A figure that size on a bill is almost always a typo."
        )

    parts = []
    for index in range(len(groups) - 1, -1, -1):  # most significant first
        value = groups[index]
        if not value:
            continue
        words = _english_under_1000(value)
        scale = _SCALES[index]
        parts.append(f"{words} {scale}".strip())
    return " ".join(parts)


def amount_in_words(paisa: int, *, currency: str = "Rupees", subunit: str = "Paisa") -> str:
    """Whole paisa -> the sentence printed under an invoice total.

        amount_in_words(0)        -> "Rupees Zero Only"
        amount_in_words(100)      -> "Rupees One Only"
        amount_in_words(12345)    -> "Rupees One Hundred Twenty Three and Forty Five Paisa Only"
        amount_in_words(-5000)    -> "Minus Rupees Fifty Only"

    The paisa part is dropped when it is zero, which is how a bill is written:
    "Rupees Fifty Only", never "Rupees Fifty and Zero Paisa Only".

    Negative amounts are prefixed rather than refused. A credit note total is a
    real thing to print, and "Minus" in front is what an accountant writes.
    """
    paisa = _as_paisa_int(paisa)
    sign = "Minus " if paisa < 0 else ""
    rupees, remainder = divmod(abs(paisa), PAISA_PER_RUPEE)

    words = f"{currency} {number_in_words(rupees)}"
    if remainder:
        words += f" and {number_in_words(remainder)} {subunit}"
    return f"{sign}{words} Only"


# ---------------------------------------------------------------------------
# Urdu
# ---------------------------------------------------------------------------
def _urdu_under_1000(number: int) -> str:
    hundreds, rest = divmod(number, 100)
    parts = []
    if hundreds:
        parts.append(f"{_URDU_UNDER_100[hundreds]} {_URDU_HUNDRED}")
    if rest:
        parts.append(_URDU_UNDER_100[rest])
    return " ".join(parts)


def number_in_words_urdu(number: int) -> str:
    """The same number in Urdu script, with the same lakh/crore grouping."""
    number = _as_paisa_int(number)
    if number < 0:
        raise MoneyError("number_in_words_urdu takes a non-negative number.")
    if number == 0:
        return _URDU_UNDER_100[0]

    groups = _indian_groups(number)
    if len(groups) > len(_URDU_SCALES):
        raise MoneyError(f"{number} is larger than this system spells out.")

    parts = []
    for index in range(len(groups) - 1, -1, -1):
        value = groups[index]
        if not value:
            continue
        parts.append(f"{_urdu_under_1000(value)} {_URDU_SCALES[index]}".strip())
    return " ".join(parts)


def amount_in_words_urdu(paisa: int) -> str:
    """Whole paisa -> the Urdu sentence, for a bilingual bill.

        amount_in_words_urdu(12345)  ->  "صرف ایک سو تئیس روپے اور پینتالیس پیسے"

    Reads right-to-left, so the "only" goes first. Rendering it needs a font
    with Arabic-script coverage — see ``apps/reports/pdf/fonts.py``, which prints
    the English line alone when no such font is vendored rather than a row of
    empty boxes.
    """
    paisa = _as_paisa_int(paisa)
    rupees, remainder = divmod(abs(paisa), PAISA_PER_RUPEE)

    words = f"{_URDU_ONLY} {number_in_words_urdu(rupees)} {_URDU_RUPEES}"
    if remainder:
        words += f" {_URDU_AND} {number_in_words_urdu(remainder)} {_URDU_PAISA}"
    return f"منفی {words}" if paisa < 0 else words


__all__ = [
    "amount_in_words",
    "amount_in_words_urdu",
    "number_in_words",
    "number_in_words_urdu",
]
