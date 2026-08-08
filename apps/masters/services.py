"""Unit conversion. **The only place this arithmetic lives.**

Storage is whole base units, always (CLAUDE.md §2). Cartons exist at exactly two
points: on the way in, where an operator types "3 cartons", and on the way out,
where a delivery sheet prints "3 ctn + 5 pcs". Between those two points there
are only pieces.

    to_base(item, qty, unit)    "3 CARTON"  -> 36  (whole base units, stored)
    from_base(item, qty_base)   36          -> (3, 0)
    fmt_qty(item, qty_base)     41          -> "3 ctn + 5 pcs"

Three rules hold everywhere in this module:

* **Nothing here is ever fractional.** Every input is an ``int`` and every
  output is an ``int``. A float or a Decimal raises rather than being rounded,
  because a quantity that needed rounding was already wrong.
* **Nothing here rounds.** ``divmod`` is exact and the remainder is returned,
  not dropped. The single rounding site in this system is
  ``apps.core.money.round_paisa`` and it is about money, not quantity.
* **Sign is preserved.** An issue and a return are negative quantities, and they
  format and convert the same way a receipt does. The invariant that
  ``cartons * carton_size + loose == qty_base`` holds for negatives too, which
  is why both halves of :func:`from_base` carry the sign.

Nowhere else in the codebase may multiply or divide by ``item.carton_size``. The
moment a second place does, one of them will disagree about the remainder and a
picker will be sent for the wrong quantity.
"""

from __future__ import annotations

from .enums import UNIT_ABBREVIATIONS, Unit
from .exceptions import InvalidPacking, InvalidQuantity, UnknownUnit


def _as_whole_units(value, name: str) -> int:
    """Validate that a quantity really is a whole number of units.

    ``bool`` is rejected explicitly because it is an ``int`` subclass, and
    silently treating ``True`` as one piece is never what the caller meant.
    ``float`` and ``Decimal`` are rejected rather than rounded: there is no half
    a piece, so a fractional quantity is a bug upstream and coercing it here
    would hide which one.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidQuantity(
            f"{name} must be a whole number of units as an int, got "
            f"{type(value).__name__}: {value!r}. Quantities are never fractional "
            f"(CLAUDE.md §2)."
        )
    return value


def _carton_size(item) -> int:
    """The item's packing, checked before anything divides by it.

    ``Item`` refuses to save a carton size below 1 and a CHECK constraint backs
    that up, so this only fires for an unsaved instance built by hand — which is
    exactly when a silent division by zero would be hardest to trace.
    """
    size = getattr(item, "carton_size", None)
    if isinstance(size, bool) or not isinstance(size, int):
        raise InvalidPacking(
            f"Item {getattr(item, 'code', item)!r} has carton_size="
            f"{size!r}; it must be a whole number of base units."
        )
    if size < 1:
        raise InvalidPacking(
            f"Item {getattr(item, 'code', item)!r} has carton_size={size}. A carton holds "
            f"at least one base unit; use 1 for an item that is not sold by the carton."
        )
    return size


def normalise_unit(unit) -> str:
    """Coerce operator input to a :class:`~apps.masters.enums.Unit` value.

    Case and surrounding whitespace are forgiven — this is a boundary, and
    "carton" from a CSV import means the same thing as ``Unit.CARTON``. An
    unrecognised unit is not forgiven: guessing produces a stock row that is
    wrong by a factor of the carton size.
    """
    if not isinstance(unit, str):
        raise UnknownUnit(
            f"Unit must be one of {', '.join(Unit.values)}, got {type(unit).__name__}: {unit!r}"
        )
    normalised = unit.strip().upper()
    if normalised not in Unit.values:
        raise UnknownUnit(f"Unknown unit {unit!r}. Valid units are: {', '.join(Unit.values)}.")
    return normalised


def unit_factor(item, unit) -> int:
    """How many base units one ``unit`` of this item is.

    Three cases, and the third is the interesting one:

    * the item's own base unit  -> 1
    * ``CARTON`` on a piece item -> ``carton_size``
    * ``PIECE`` on a carton item -> raises

    That last case is a request for a fraction of a stored unit. An item whose
    base unit is the carton has no piece to count, so there is no honest integer
    to return and returning one anyway would silently understate the movement.

    Note that ``CARTON`` against an item with ``carton_size = 1`` is **allowed**
    and is worth 1. A carton of one is arithmetically unambiguous, and refusing
    it would make every import and every keyboard entry branch on
    :attr:`~apps.masters.models.Item.allows_carton` before it could convert.
    Display is where that distinction matters, and :func:`from_base` enforces it
    there.
    """
    normalised = normalise_unit(unit)
    size = _carton_size(item)

    if normalised == item.base_unit:
        return 1
    if normalised == Unit.CARTON:
        return size
    raise UnknownUnit(
        f"Item {item.code} is counted in {item.base_unit}; {normalised} is not a unit it "
        f"can be entered in. A fraction of a base unit is not a quantity (CLAUDE.md §2)."
    )


def to_base(item, qty, unit=Unit.PIECE) -> int:
    """Entered quantity -> whole base units, for storage.

    This is what turns what an operator typed into the integer a stock or order
    line holds::

        to_base(carton_of_12, 3, "CARTON")  -> 36
        to_base(carton_of_12, 5, "PIECE")   -> 5
        to_base(loose_item, 5, "CARTON")    -> 5   (carton_size == 1)

    Negative quantities pass straight through, so a return of two cartons is
    ``to_base(item, -2, "CARTON")``. The sign is the caller's business; the
    conversion does not care which direction stock is moving.
    """
    return _as_whole_units(qty, "qty") * unit_factor(item, unit)


def from_base(item, qty_base) -> tuple[int, int]:
    """Stored base units -> ``(cartons, loose_pieces)``, for display.

    Exact and lossless in both directions::

        cartons * item.carton_size + loose == qty_base

    holds for every input, including negatives, where both halves carry the
    sign: ``-17`` at a carton size of 12 is ``(-1, -5)``, and ``-12 + -5`` is
    ``-17``.

    An item that is not cartoned — ``carton_size == 1``, so
    :attr:`~apps.masters.models.Item.allows_carton` is False — always returns
    ``(0, qty_base)``. Dividing by one would technically give ``(17, 0)``, and
    "17 cartons" of a 25kg rice bag is how a picker loads seventeen pallets.
    """
    qty_base = _as_whole_units(qty_base, "qty_base")
    size = _carton_size(item)

    if not item.allows_carton:
        return 0, qty_base

    # divmod on the absolute value, then re-sign: Python floors, so
    # divmod(-17, 12) is (-2, 7), which reads as "minus two cartons plus seven
    # loose" and is not what anyone means by minus seventeen pieces.
    sign = -1 if qty_base < 0 else 1
    cartons, loose = divmod(abs(qty_base), size)
    return sign * cartons, sign * loose


def fmt_qty(item, qty_base) -> str:
    """Stored base units -> a string a human reads on a delivery sheet.

        fmt_qty(carton_of_12, 41)  -> "3 ctn + 5 pcs"
        fmt_qty(carton_of_12, 36)  -> "3 ctn"
        fmt_qty(carton_of_12, 5)   -> "5 pcs"
        fmt_qty(loose_item, 17)    -> "17 pcs"
        fmt_qty(any_item, 0)       -> "0 pcs"

    Display only. Never parse this back — :func:`to_base` is the way in.
    """
    qty_base = _as_whole_units(qty_base, "qty_base")
    if qty_base < 0:
        # One minus sign in front of the whole quantity, rather than one on each
        # half: "-3 ctn + -5 pcs" reads as a subtraction that it is not.
        return f"-{fmt_qty(item, -qty_base)}"

    # Through normalise_unit rather than straight into the dict, so an item
    # carrying a unit nobody defined fails with a sentence instead of a KeyError.
    base_label = UNIT_ABBREVIATIONS[normalise_unit(item.base_unit)]
    carton_label = UNIT_ABBREVIATIONS[Unit.CARTON]
    cartons, loose = from_base(item, qty_base)

    if not cartons:
        return f"{loose} {base_label}"
    if not loose:
        return f"{cartons} {carton_label}"
    return f"{cartons} {carton_label} + {loose} {base_label}"
