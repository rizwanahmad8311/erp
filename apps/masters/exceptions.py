"""Masters invariant violations.

Like the core and accounting exceptions these are **not** ``ValidationError``:
they mean a caller asked for something that cannot be expressed, not that an
operator mistyped a form field. A unit conversion that cannot be done is a bug
in the calling code, and it should reach a 500 and a log line rather than be
quietly coerced into a number that is wrong.

Everything here inherits :class:`apps.core.exceptions.CoreError`, so a caller
that wants to catch "any broken invariant" can do so with one ``except``.
"""

from apps.core.exceptions import CoreError


class MastersError(CoreError):
    """Base class for every invariant violation raised by apps.masters."""


class UnknownUnit(MastersError):
    """Raised when a quantity is offered in a unit the item is not sold in.

    Two cases reach this. A unit that is not in :class:`~apps.masters.enums.Unit`
    at all, and ``PIECE`` against an item whose base unit is the carton — the
    second one is a request for a fraction of a stored unit, and there is no
    such thing (CLAUDE.md §2).
    """


class InvalidQuantity(MastersError):
    """Raised when a quantity is not a whole number of units.

    A ``float``, a ``Decimal``, a numeric string. Quantities are ``int`` from
    the moment they are parsed; there is no 2.5 pieces and no 1.5 cartons.
    """


class InvalidPacking(MastersError):
    """Raised when an item's packing cannot be converted with.

    ``carton_size`` below 1 — a carton that holds nothing, or holds a negative
    number of things — or a carton size above 1 on an item whose base unit is
    already the carton. The database CHECK constraints make both unreachable;
    this is what the conversion helpers raise if one ever gets past them, since
    the alternative is dividing by zero halfway through a stock posting.
    """


class InvalidCategory(MastersError):
    """Raised when a category change would break the tree: self-parenting, or a cycle."""


class DuplicatePrimarySeller(MastersError):
    """Raised when a second seller is flagged primary on the same route.

    "The route's seller" has to be one person — a commission report cannot split
    a route between two of them and a delivery sheet cannot be headed by both.
    """


class InvalidLine(MastersError):
    """Raised when a document line cannot be turned into a quantity and an amount.

    A quantity of zero or less, a negative rate, a discount larger than the
    line, a tax rate over 100%. All of them are arithmetic that cannot produce a
    posting, and all of them are caught before anything is written.

    Lives here rather than in a transaction app because
    :func:`apps.masters.pricing.compute_line` lives here: purchasing and sales
    both raise it, and an exception defined twice is an exception that only half
    the ``except`` clauses catch.
    """
