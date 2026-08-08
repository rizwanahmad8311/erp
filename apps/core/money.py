"""
Paisa <-> human conversion. Formatting is a *display* concern: nothing here is
ever used to compute a stored value. Arithmetic happens on integer paisa.
"""

from decimal import ROUND_HALF_UP, Decimal

PAISA_PER_RUPEE = 100


def to_paisa(rupees) -> int:
    """Parse operator/import input (str, int, Decimal) into integer paisa.

    Decimal is used here only as a parsing tool at the system boundary; the
    return value is an int and the Decimal never reaches the database.
    """
    amount = Decimal(str(rupees)) * PAISA_PER_RUPEE
    return int(amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def to_rupees(paisa: int) -> Decimal:
    """Paisa -> Decimal rupees, for display and for reportlab output only."""
    return (Decimal(int(paisa)) / PAISA_PER_RUPEE).quantize(Decimal("0.01"))


def format_money(paisa: int, symbol: str = "Rs", thousands: bool = True) -> str:
    """Render paisa as a human string, e.g. -12345 -> 'Rs -123.45'."""
    value = to_rupees(paisa)
    formatted = f"{value:,.2f}" if thousands else f"{value:.2f}"
    return f"{symbol} {formatted}".strip()


def split_evenly(paisa: int, parts: int) -> list[int]:
    """Split an amount into `parts` integers that sum back to exactly `paisa`.

    Used for proportional allocation (discounts, freight, tax) where dropping a
    remainder would leave the ledger unbalanced by a paisa.
    """
    if parts <= 0:
        raise ValueError("parts must be positive")
    base, remainder = divmod(abs(paisa), parts)
    sign = -1 if paisa < 0 else 1
    return [sign * (base + (1 if i < remainder else 0)) for i in range(parts)]
