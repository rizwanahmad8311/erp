"""
Money primitives. Everything monetary in this system is an integer number of
paisa; this module is the only place that knows how to get in and out of that
representation.

Three boundary functions and one value object:

    to_paisa(value) -> int      parse operator/import input into stored paisa
    to_rupees(paisa) -> Decimal display only, never fed back into a calculation
    fmt(paisa) -> str           display only, "1,234.50"
    Money                       arithmetic inside posting services

Decimal appears here and nowhere else. It is a parsing and formatting tool at
the system boundary; it never reaches a model field. See CLAUDE.md §1.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_FLOOR, ROUND_HALF_EVEN, Decimal, DecimalException, InvalidOperation

from .exceptions import MoneyError

PAISA_PER_RUPEE = 100

# The one and only rounding mode in this codebase.
ROUNDING = ROUND_HALF_EVEN

_ONE = Decimal(1)
_HUNDREDTHS = Decimal("0.01")

# Digits, one optional sign, optional thousands commas, optional decimals.
_NUMERIC_RE = re.compile(r"^[+-]?[\d,]*\.?\d*$")


def round_paisa(amount: Decimal) -> int:
    """Round a Decimal number of paisa to a whole paisa. **The single rounding
    point in the entire system.**

    Uses banker's rounding (``ROUND_HALF_EVEN``): an exact half goes to the
    nearest *even* integer, so 0.5 -> 0, 1.5 -> 2, 2.5 -> 2, 3.5 -> 4.

    Why banker's rather than half-up: half-up is biased away from zero, so on a
    long run of half-paisa remainders — which is exactly what percentage
    discounts and tax on many small lines produce — the error accumulates in one
    direction and a day's sales drift measurably against the ledger. Half-even
    splits the halves both ways and the bias cancels.

    Every other function here, and every rounding in any service, must go
    through this function. If you find yourself writing ``round()`` or
    ``quantize()`` on money anywhere else, that is a bug.
    """
    if not isinstance(amount, Decimal):
        raise MoneyError(f"round_paisa expects a Decimal, got {type(amount).__name__}")
    try:
        return int(amount.quantize(_ONE, rounding=ROUNDING))
    except (DecimalException, ValueError) as exc:
        # ValueError is not redundant. Infinity raises InvalidOperation from
        # quantize() and is a DecimalException, but **NaN quantizes happily** —
        # it returns Decimal('NaN'), and it is int() that then raises
        # ValueError. Catching only DecimalException let a NaN out of here as a
        # raw traceback, which is the one thing this function exists to prevent.
        raise MoneyError(f"Cannot round {amount!r} to whole paisa") from exc


def _to_decimal(value) -> Decimal:
    """Coerce accepted input types to an exact Decimal in rupees.

    ``float`` is rejected on purpose: 0.1 + 0.2 is not 0.3, and a float that
    reaches this function has usually already lost the precision we are trying
    to protect. Pass a string or a Decimal.
    """
    if isinstance(value, bool):
        # bool is an int subclass; silently treating True as 1 rupee is never
        # what the caller meant.
        raise MoneyError("Cannot interpret a bool as money")

    if isinstance(value, Decimal):
        if not value.is_finite():
            raise MoneyError(f"Cannot interpret {value!r} as money")
        return value

    if isinstance(value, int):
        return Decimal(value)

    if isinstance(value, float):
        raise MoneyError(
            f"Refusing to convert float {value!r} to money — floats are not exact. "
            f'Pass a string ("{value}") or a Decimal.'
        )

    if isinstance(value, str):
        text = value.strip().replace(" ", "")
        if not text:
            raise MoneyError("Cannot interpret an empty string as money")
        if not _NUMERIC_RE.match(text):
            raise MoneyError(f"Cannot interpret {value!r} as money")
        text = text.replace(",", "")
        # "-" / "." / "-." survive the regex but are not numbers.
        try:
            return Decimal(text)
        except InvalidOperation as exc:
            raise MoneyError(f"Cannot interpret {value!r} as money") from exc

    raise MoneyError(f"Cannot interpret {type(value).__name__} as money")


def to_paisa(rupees) -> int:
    """Parse operator or import input in **rupees** into stored integer paisa.

    Accepts ``str`` (commas and surrounding whitespace allowed), ``Decimal`` and
    ``int``. Rejects ``float`` — see :func:`_to_decimal`.

        to_paisa("1,234.50")  -> 123450
        to_paisa("-99.99")    -> -9999
        to_paisa("0.005")     -> 0       (half a paisa, banker's rounds to even)

    Input with more than two decimal places is rounded to whole paisa through
    :func:`round_paisa`.
    """
    return round_paisa(_to_decimal(rupees) * PAISA_PER_RUPEE)


def to_rupees(paisa: int) -> Decimal:
    """Paisa -> Decimal rupees. **Display only.**

    The result is exact (paisa are integral, so the division by 100 always
    terminates), but it must never be fed back into a stored calculation —
    round-tripping through rupees is how fractional currency creeps in.
    """
    return (Decimal(_as_paisa_int(paisa)) / PAISA_PER_RUPEE).quantize(_HUNDREDTHS)


def fmt(paisa: int, *, thousands: bool = True) -> str:
    """Render paisa for humans: ``123450 -> "1,234.50"``. **Display only.**

    No currency symbol — invoices and reports place that themselves, and amount
    columns align better without it. Use :func:`format_money` when a symbol is
    genuinely wanted inline.
    """
    value = to_rupees(paisa)
    return f"{value:,.2f}" if thousands else f"{value:.2f}"


def format_money(paisa: int, symbol: str = "Rs", *, thousands: bool = True) -> str:
    """``fmt`` with a currency symbol: ``-12345 -> "Rs -123.45"``."""
    return f"{symbol} {fmt(paisa, thousands=thousands)}".strip()


def _as_paisa_int(value) -> int:
    """Validate that a value really is a whole number of paisa."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise MoneyError(f"Expected whole paisa as an int, got {type(value).__name__}: {value!r}")
    return value


def split_evenly(paisa: int, parts: int) -> list[int]:
    """Split an amount into ``parts`` integers that sum back to exactly ``paisa``.

    The remainder is spread one paisa at a time across the leading parts, so
    nothing is lost. Prefer :meth:`Money.allocate` when the split is weighted.
    """
    if parts <= 0:
        raise MoneyError("parts must be positive")
    paisa = _as_paisa_int(paisa)
    base, remainder = divmod(abs(paisa), parts)
    sign = -1 if paisa < 0 else 1
    return [sign * (base + (1 if i < remainder else 0)) for i in range(parts)]


@dataclass(frozen=True, order=True, slots=True)
class Money:
    """An exact monetary amount, for arithmetic inside posting services.

    Wraps an integer number of paisa. Immutable, so it can be shared freely and
    can never be mutated halfway through a posting.

    Model fields still store and return plain ``int`` paisa —
    :class:`~apps.core.fields.MoneyField` does not convert. Wrap at the top of a
    service, unwrap with ``.paisa`` when writing rows back:

        total = Money(line.amount_paisa) + Money(freight.amount_paisa)
        entry.amount_paisa = total.paisa

    Addition and subtraction only work between two ``Money`` values; mixing in a
    bare int raises, which is what catches "is this paisa or rupees?" bugs.
    """

    paisa: int

    def __post_init__(self):
        object.__setattr__(self, "paisa", _as_paisa_int(self.paisa))

    # -- constructors ------------------------------------------------------
    @classmethod
    def zero(cls) -> Money:
        return cls(0)

    @classmethod
    def from_rupees(cls, rupees) -> Money:
        """``Money.from_rupees("1,234.50")`` -> ``Money(123450)``."""
        return cls(to_paisa(rupees))

    # -- display -----------------------------------------------------------
    @property
    def rupees(self) -> Decimal:
        """Display only — see :func:`to_rupees`."""
        return to_rupees(self.paisa)

    def __str__(self) -> str:
        return fmt(self.paisa)

    def __repr__(self) -> str:
        return f"Money({self.paisa})"  # paisa, so repr is never ambiguous

    def __bool__(self) -> bool:
        return self.paisa != 0

    # -- arithmetic --------------------------------------------------------
    def __add__(self, other: Money) -> Money:
        if not isinstance(other, Money):
            return NotImplemented
        return Money(self.paisa + other.paisa)

    def __radd__(self, other):
        # Lets sum() start from 0 without special-casing at every call site.
        if other == 0:
            return self
        return NotImplemented

    def __sub__(self, other: Money) -> Money:
        if not isinstance(other, Money):
            return NotImplemented
        return Money(self.paisa - other.paisa)

    def __neg__(self) -> Money:
        """The reversing amount. Cancellations post ``-entry.amount``."""
        return Money(-self.paisa)

    def __abs__(self) -> Money:
        return Money(abs(self.paisa))

    def __mul__(self, factor) -> Money:
        """Multiply by a quantity (``int``, exact) or a rate (``Decimal``).

        A Decimal factor rounds **once**, through :func:`round_paisa`. Never
        chain multiplications expecting exactness — compute the whole factor
        first, then multiply once.
        """
        if isinstance(factor, bool):
            return NotImplemented
        if isinstance(factor, int):
            return Money(self.paisa * factor)
        if isinstance(factor, Decimal):
            return Money(round_paisa(Decimal(self.paisa) * factor))
        return NotImplemented

    __rmul__ = __mul__

    def percent(self, rate) -> Money:
        """``Money(10000).percent("15")`` -> 15% of the amount, rounded once."""
        return Money(round_paisa(Decimal(self.paisa) * _to_decimal(rate) / 100))

    # -- allocation --------------------------------------------------------
    def split(self, parts: int) -> list[Money]:
        """Even split whose parts sum back to exactly this amount."""
        return [Money(p) for p in split_evenly(self.paisa, parts)]

    def allocate(self, weights) -> list[Money]:
        """Weighted split whose parts sum back to exactly this amount.

        Used for spreading a header-level discount, freight or tax across lines
        in proportion to their value. The largest-remainder method assigns the
        leftover paisa to the parts that were rounded down hardest, so the
        result is stable and never loses a paisa.
        """
        weights = [_to_decimal(w) for w in weights]
        if not weights:
            raise MoneyError("allocate needs at least one weight")
        if any(w < 0 for w in weights):
            raise MoneyError("allocate weights must be non-negative")
        total = sum(weights)
        if total == 0:
            raise MoneyError("allocate weights must not sum to zero")

        exact = [Decimal(self.paisa) * w / total for w in weights]
        floors = [int(e.to_integral_value(rounding=ROUND_FLOOR)) for e in exact]
        leftover = self.paisa - sum(floors)

        # Hand the remaining paisa to the largest fractional parts first.
        order = sorted(range(len(exact)), key=lambda i: exact[i] - floors[i], reverse=True)
        step = 1 if leftover > 0 else -1
        for i in range(abs(leftover)):
            floors[order[i % len(order)]] += step
        return [Money(p) for p in floors]
