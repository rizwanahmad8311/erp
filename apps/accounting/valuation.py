"""
Moving weighted average valuation, as arithmetic. No database, no Django.

A stock position is two numbers — how many base units are held, and how many
paisa of cost sit behind them — and every valuation question is answered from
that pair:

    rate    = value / qty          what one base unit is currently worth
    receive = qty x rate           incoming is valued at what it cost
    issue   = a share of value     outgoing is valued at the average now

Kept separate from :mod:`apps.accounting.services` because this is the part that
has to be *right*, and it is much easier to be sure of that when it can be
exercised without a posting, a voucher or a transaction. The service is then
only responsible for reading the prior rows, calling this, and writing what came
back.

Two rules are load-bearing and are the reason ``issue`` is not simply
``qty * rate``:

* **A full sweep leaves exactly zero.** Issuing every unit on hand hands back
  the whole stored value, to the paisa. Valuing it at a rounded average instead
  would strand a few paisa of inventory value against a quantity of nothing —
  a balance sheet line that cannot be explained and cannot be cleared.
* **Value, not rate, is the figure that adds up.** ``rate_paisa`` on a row is
  the average as it stood, recorded so history can be read back without
  recomputing it. ``value_paisa`` is what the balance is summed from. Where
  rounding makes the two disagree by a paisa, value wins.

Both of those come free from :meth:`apps.core.money.Money.allocate`, which is
what an issue actually is: a proportional split of the value held, between what
leaves and what stays, whose parts sum back to the original exactly
(CLAUDE.md §1). The one division in this module is the average itself, and it
goes through :func:`~apps.core.money.round_paisa` like every other rounding in
the system.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import NamedTuple

from apps.core.money import Money, round_paisa

from .exceptions import InvalidPosting


class Movement(NamedTuple):
    """One valued stock movement, ready to be written onto a row.

    ``qty_base`` is signed the way the ledger stores it: positive in, negative
    out. ``value_paisa`` carries the same sign. ``rate_paisa`` never does — a
    rate is a per-unit fact, not a direction.
    """

    qty_base: int
    rate_paisa: int
    value_paisa: int


@dataclass(frozen=True, slots=True)
class Position:
    """What one ``(item, warehouse)`` pair holds: a quantity and its cost.

    Immutable. :meth:`apply` returns the next position rather than mutating this
    one, which is what lets a caller walk a voucher's lines in order without
    ever being able to lose track of where it was.
    """

    qty_base: int = 0
    value_paisa: int = 0

    @property
    def rate_paisa(self) -> int:
        """The moving weighted average, in paisa per base unit.

        Zero when there is nothing to average from — an empty position, an
        oversold one, or one whose value has gone under water. Callers that need
        a rate in those cases supply their own fallback; see :meth:`issue`.
        """
        if self.qty_base <= 0 or self.value_paisa <= 0:
            return 0
        return round_paisa(Decimal(self.value_paisa) / self.qty_base)

    def apply(self, movement: Movement) -> Position:
        """The position after ``movement`` lands."""
        return Position(
            qty_base=self.qty_base + movement.qty_base,
            value_paisa=self.value_paisa + movement.value_paisa,
        )

    # ------------------------------------------------------------------
    # The two movements
    # ------------------------------------------------------------------
    def receive(self, qty_base: int, rate_paisa: int) -> Movement:
        """Value ``qty_base`` units coming in at ``rate_paisa`` each.

        Incoming stock is valued at what it actually cost — that cost is the
        *input* to the average, never derived from it. The multiplication is
        exact (two integers), so nothing is rounded here and the average moves
        only when it is next read.

        A rate of zero is allowed and is not a mistake: free goods and bonus
        cartons are real, they genuinely cost nothing, and they genuinely drag
        the average down.
        """
        _assert_positive(qty_base, "qty_base")
        if isinstance(rate_paisa, bool) or not isinstance(rate_paisa, int):
            raise InvalidPosting(
                f"rate_paisa must be whole paisa as an int, got "
                f"{type(rate_paisa).__name__}: {rate_paisa!r}"
            )
        if rate_paisa < 0:
            raise InvalidPosting(
                f"rate_paisa is {rate_paisa}; a cost rate is never negative. "
                f"Stock coming back out is an issue, not a receipt at a minus rate."
            )
        return Movement(
            qty_base=qty_base,
            rate_paisa=rate_paisa,
            value_paisa=(Money(rate_paisa) * qty_base).paisa,
        )

    def receive_at_value(self, qty_base: int, value_paisa: int) -> Movement:
        """Value ``qty_base`` units coming in at a **known total cost**.

        The counterpart to :meth:`receive`, for the case where what is known
        exactly is the money rather than the rate. A supplier bills "10 cartons
        at Rs 2,400", which is Rs 24,000 and not a paisa more; at 24 pieces to
        the carton that is 240 pieces at Rs 100 exactly, but at a bill rate of
        Rs 2,500 it is 240 pieces at 1041.66... paisa, and **no** integer rate
        multiplies back to the bill.

        So the total is taken as given and the rate is derived from it:
        ``value_paisa`` is what inventory is debited and what the position is
        summed from, and ``rate_paisa`` is the average it works out to, recorded
        so a stock card can be read back. Where the two disagree by a paisa,
        value is the figure that counts — the module docstring's second rule,
        and the reason :class:`~apps.accounting.models.StockEntry` says the same
        thing about its own columns.

        The alternative — rounding the rate and letting inventory be debited
        ``qty x rate`` — puts a few paisa into stock that nobody paid for, and
        leaves the purchase invoice's own general ledger unable to balance
        without a plug.
        """
        _assert_positive(qty_base, "qty_base")
        if isinstance(value_paisa, bool) or not isinstance(value_paisa, int):
            raise InvalidPosting(
                f"value_paisa must be whole paisa as an int, got "
                f"{type(value_paisa).__name__}: {value_paisa!r}"
            )
        if value_paisa < 0:
            raise InvalidPosting(
                f"value_paisa is {value_paisa}; incoming stock never carries value out. "
                f"Goods going back to a supplier are an issue, not a receipt at a minus value."
            )
        return Movement(
            qty_base=qty_base,
            # The average this works out to, for the card. Rounded once, through
            # the single rounding point, and never multiplied back out.
            rate_paisa=round_paisa(Decimal(value_paisa) / qty_base),
            value_paisa=value_paisa,
        )

    def issue(self, qty_base: int, *, fallback_rate_paisa: int = 0) -> Movement:
        """Value ``qty_base`` units going out at the average as it stands now.

        ``qty_base`` is the magnitude issued, given positive; the returned
        movement is negative on both quantity and value.

        The value taken out is the issued *share of what is held* — an
        allocation of the stored value between what leaves and what stays,
        rather than ``qty x rate``. The two parts sum back to the stored value
        exactly, so when the whole position goes the whole value goes with it
        and the position lands on zero. That is the single reason this is not a
        multiplication.

        ``fallback_rate_paisa`` is used only when there is no average to take:
        an empty, oversold or under-water position. That is reachable solely on
        an installation with ``ALLOW_NEGATIVE_STOCK`` on; everywhere else the
        service refuses the issue before it gets here. An under-water position
        issues at zero rather than handing back value it does not have, so the
        deficit stays visible in the balance instead of being quietly spread
        over the next few issues.

        Issuing more than is held is likewise only reachable with that setting
        on: the units that exist go at their real value, and the overdraft goes
        at the rate that was current when it was taken.
        """
        _assert_positive(qty_base, "qty_base")

        if self.qty_base > 0 and self.value_paisa > 0:
            rate_paisa = self.rate_paisa
            on_hand = min(qty_base, self.qty_base)
            # Split the value held between what leaves and what stays. The two
            # parts sum back to it exactly, so when on_hand is the whole
            # position the first part is the whole value and nothing is left
            # behind — no remainder to drop (CLAUDE.md §1).
            leaving, _staying = Money(self.value_paisa).allocate([on_hand, self.qty_base - on_hand])
            value = leaving + Money(rate_paisa) * (qty_base - on_hand)
        else:
            rate_paisa = max(fallback_rate_paisa, 0)
            value = Money(rate_paisa) * qty_base

        return Movement(
            qty_base=-qty_base,
            rate_paisa=rate_paisa,
            value_paisa=-value.paisa,
        )

    def __str__(self) -> str:
        return f"{self.qty_base} @ {self.rate_paisa} = {self.value_paisa}"


def _assert_positive(qty_base, label: str) -> int:
    """A movement magnitude is a positive whole number of base units.

    Fractions are refused outright rather than rounded (CLAUDE.md §2): there is
    no half a piece, and a float arriving here means something upstream did
    division it should not have.
    """
    if isinstance(qty_base, bool) or not isinstance(qty_base, int):
        raise InvalidPosting(
            f"{label} must be whole base units as an int, got "
            f"{type(qty_base).__name__}: {qty_base!r}"
        )
    if qty_base <= 0:
        raise InvalidPosting(f"{label} must be a positive magnitude, got {qty_base}.")
    return qty_base


__all__ = ["Movement", "Position"]
