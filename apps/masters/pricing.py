"""What a quantity of an item at a rate comes to. **The only place this lives.**

Extracted here rather than written once per transaction app, because it is not a
purchasing rule or a sales rule — it is a question about an *item*, and every
argument below is either an item or a number. A supplier bills by the carton and
a shop buys by the carton; the stock ledger stores pieces either way; and the
arithmetic that reconciles those is identical in both directions. Two copies of
it would be two copies that drift, and the one that drifts is the one nobody is
looking at.

The rounding rule
-----------------
    **The money that was agreed is exact. The per-base-unit rate is derived.**

``amount_paisa`` is ``qty_input * rate_input_paisa`` — an integer times an
integer, so there is nothing to round and nothing is rounded. ``rate_paisa`` is
``amount_paisa / qty_base`` put through :func:`~apps.core.money.round_paisa`
**once**, and it is recorded for the stock card rather than multiplied back out.

Ten cartons of twelve at Rs 2,400 is 120 pieces at exactly Rs 200 and the two
agree. Ten cartons of twenty-four at Rs 2,500 is 240 pieces at 1041.66... paisa
and **no integer rate multiplies back to Rs 25,000**. When that happens the
document is right and the rate is a rounded figure — which is why every posting
service moves stock at the line's *value* rather than at its rate, and why the
general ledger and the stock ledger agree to the paisa on every line, always.

Doing it the other way round — rate per piece first, amount from
``qty_base * rate_paisa`` — puts the document out by up to half a paisa per
piece, which is Rs 1.20 on a 240-piece line that nobody agreed to.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from apps.core.money import Money, fmt, round_paisa

from .enums import BASIS_POINTS_PER_UNIT, Unit
from .exceptions import InvalidLine
from .services import to_base


@dataclass(frozen=True, slots=True)
class LineAmounts:
    """Everything a document line stores, computed from what was typed.

    Immutable, and deliberately not a model instance: this is the arithmetic on
    its own, so it can be tested without a database, a document or a party.
    """

    qty_base: int
    rate_paisa: int
    amount_paisa: int
    discount_paisa: int
    tax_paisa: int

    @property
    def net_paisa(self) -> int:
        """After discount, before tax. What the goods actually came to."""
        return self.amount_paisa - self.discount_paisa

    @property
    def total_paisa(self) -> int:
        return self.net_paisa + self.tax_paisa

    @property
    def rate_is_exact(self) -> bool:
        """Whether ``qty_base * rate_paisa`` lands back on ``amount_paisa``.

        False is normal, not a fault — see the module docstring. The entry
        screens show it so nobody reads a stock card and thinks the document is
        wrong.
        """
        return self.qty_base * self.rate_paisa == self.amount_paisa

    @property
    def rate_drift_paisa(self) -> int:
        """How far ``qty_base * rate_paisa`` is from the document. Never posted."""
        return self.qty_base * self.rate_paisa - self.amount_paisa


def compute_line(
    item,
    *,
    qty_input: int,
    unit_input: str = Unit.PIECE,
    rate_input_paisa: int,
    discount_paisa: int = 0,
    tax_rate_bp: int | None = None,
) -> LineAmounts:
    """Turn "10 cartons at Rs 2,400 each" into the five numbers a line stores.

    ``rate_input_paisa`` is the rate **in the unit that was typed** — per carton
    when ``unit_input`` is CARTON. That is what is quoted, what is printed, and
    the only rate an operator ever sees on an entry screen.

    The order of operations is the point:

    1. ``amount_paisa = qty_input * rate_input_paisa`` — exact, and the anchor.
       Nothing downstream is allowed to change it.
    2. ``qty_base`` from :func:`apps.masters.services.to_base`, which is the only
       place the carton size is ever applied.
    3. ``rate_paisa`` from the amount, rounded **once**. Derived, for the stock
       card. Where it does not multiply back exactly, the amount is right.
    4. tax on the discounted amount, rounded **once**.

    ``tax_rate_bp`` defaults to the item's own rate. Pass it explicitly only
    when a document genuinely charges something else.
    """
    qty_input = _as_positive_int(qty_input, "qty_input")
    rate_input_paisa = _as_non_negative_int(rate_input_paisa, "rate_input_paisa")
    discount_paisa = _as_non_negative_int(discount_paisa, "discount_paisa")

    # to_base validates the unit and refuses a fraction of a base unit.
    qty_base = to_base(item, qty_input, unit_input)

    # Two integers. There is nothing here to round, and that is the whole design.
    amount_paisa = qty_input * rate_input_paisa

    if discount_paisa > amount_paisa:
        raise InvalidLine(
            f"Discount of {fmt(discount_paisa)} is more than the line amount of "
            f"{fmt(amount_paisa)}. A line cannot come to less than nothing."
        )

    # The one division on the line, through the one rounding point in the system.
    rate_paisa = round_paisa(Decimal(amount_paisa) / qty_base)

    if tax_rate_bp is None:
        tax_rate_bp = getattr(item, "tax_rate_bp", 0)
    tax_rate_bp = _as_non_negative_int(tax_rate_bp, "tax_rate_bp")
    if tax_rate_bp > BASIS_POINTS_PER_UNIT:
        raise InvalidLine(f"Tax rate of {tax_rate_bp} basis points is over 100%. 1750 is 17.5%.")

    # Money.percent rounds once, through round_paisa. bp/100 is exact in Decimal.
    taxable = Money(amount_paisa - discount_paisa)
    tax_paisa = taxable.percent(Decimal(tax_rate_bp) / 100).paisa

    return LineAmounts(
        qty_base=qty_base,
        rate_paisa=rate_paisa,
        amount_paisa=amount_paisa,
        discount_paisa=discount_paisa,
        tax_paisa=tax_paisa,
    )


def entry_rate_paisa(line) -> int:
    """The rate the operator typed, recovered from a saved line.

    ``amount_paisa`` is ``qty_input * rate_input_paisa``, so this division is
    always exact — there is no rounding here and there must not be. It is what
    lets an entry screen re-show a draft line as "10 cartons @ 2,400" rather
    than as the derived per-piece figure nobody typed.
    """
    if not line.qty_input:
        return 0
    quotient, remainder = divmod(line.amount_paisa, line.qty_input)
    if remainder:  # pragma: no cover - only reachable if amount was written by hand
        raise InvalidLine(
            f"Line amount {line.amount_paisa} is not a whole multiple of qty_input "
            f"{line.qty_input}; it was not produced by compute_line()."
        )
    return quotient


def apply_line_amounts(line, amounts: LineAmounts):
    """Copy a :class:`LineAmounts` onto a line instance. Does not save.

    The low-level half of :func:`update_line`. Prefer that one: this writes the
    *derived* fields only, so on its own it will happily leave ``qty_input``
    describing one quantity and ``amount_paisa`` describing another.
    """
    line.qty_base = amounts.qty_base
    line.rate_paisa = amounts.rate_paisa
    line.amount_paisa = amounts.amount_paisa
    line.discount_paisa = amounts.discount_paisa
    line.tax_paisa = amounts.tax_paisa
    return line


def update_line(
    line,
    *,
    item,
    qty_input: int,
    unit_input: str,
    rate_input_paisa: int,
    discount_paisa: int = 0,
    tax_rate_bp: int | None = None,
):
    """Write everything a line holds, from what the operator typed. Does not save.

    **The way to fill in a line.** It sets what was typed *and* what was derived
    from it in one call, so the two can never describe different quantities.
    Setting the amounts alone would leave a line reading "10 cartons" and
    costing what six cartons cost — and since every line model recomputes
    ``qty_base`` from ``qty_input`` on save, the quantity that actually posted
    would be the ten.
    """
    line.item = item
    line.qty_input = qty_input
    line.unit_input = unit_input
    return apply_line_amounts(
        line,
        compute_line(
            item,
            qty_input=qty_input,
            unit_input=unit_input,
            rate_input_paisa=rate_input_paisa,
            discount_paisa=discount_paisa,
            tax_rate_bp=tax_rate_bp,
        ),
    )


def _as_positive_int(value, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidLine(
            f"{label} must be a whole number as an int, got {type(value).__name__}: {value!r}"
        )
    if value <= 0:
        raise InvalidLine(f"{label} is {value}; a document line moves a positive quantity.")
    return value


def _as_non_negative_int(value, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidLine(
            f"{label} must be whole paisa as an int, got {type(value).__name__}: {value!r}. "
            f"Run operator input through apps.core.money.to_paisa first."
        )
    if value < 0:
        raise InvalidLine(f"{label} is {value}; it cannot be negative.")
    return value


__all__ = [
    "LineAmounts",
    "apply_line_amounts",
    "compute_line",
    "entry_rate_paisa",
    "update_line",
]
