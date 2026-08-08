"""
The two soft links a ledger row carries.

Neither is a foreign key, and that is the point. A ledger row must outlive both
the document that caused it and the party it was raised against — CLAUDE.md §3
says the row is history, and history cannot be held hostage by a master record
someone wants to tidy up in 2031.

What a soft link loses is the database's referential check, so the value objects
here do that checking at the boundary instead: a party is a ``(type, id)`` pair
or it is nothing, and a voucher has been saved and has a real document code
before a single row is written against it.
"""

from __future__ import annotations

from dataclasses import dataclass

from .enums import PartyType
from .exceptions import InvalidPosting


def _assert_positive_id(value, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidPosting(f"{label} must be an int, got {type(value).__name__}: {value!r}")
    if value <= 0:
        raise InvalidPosting(f"{label} must be a positive row id, got {value!r}")
    return value


@dataclass(frozen=True, slots=True)
class PartyRef:
    """Which client or vendor a receivable/payable line belongs to.

    Constructed rather than passed as two loose arguments so that a half-set
    party — a type with no id, an id with no type — cannot be built at all. The
    database has the same rule as a CHECK constraint; this is where it fails
    with a sentence instead of an ``IntegrityError``.
    """

    type: str
    id: int

    def __post_init__(self):
        if self.type not in PartyType.values:
            raise InvalidPosting(
                f"Unknown party type {self.type!r}; expected one of {sorted(PartyType.values)}."
            )
        _assert_positive_id(self.id, "party id")

    @classmethod
    def coerce(cls, value) -> PartyRef | None:
        """Accept ``None``, a :class:`PartyRef`, or a ``(type, id)`` pair."""
        if value is None:
            return None
        if isinstance(value, cls):
            return value
        if isinstance(value, tuple | list) and len(value) == 2:
            return cls(value[0], value[1])
        raise InvalidPosting(f"party must be a PartyRef, a (type, id) pair, or None; got {value!r}")

    def __str__(self) -> str:
        return f"{self.type}#{self.id}"


@dataclass(frozen=True, slots=True)
class VoucherRef:
    """Which document caused a ledger row.

    ``code`` is denormalised onto every row on purpose (CLAUDE.md §6): a ledger
    report that has to join out to a dozen document tables to print "SI-2026-
    000123" is a report that will be rewritten as a cached total by the first
    person who finds it slow.
    """

    type: str
    id: int
    code: str

    @classmethod
    def of(cls, voucher) -> VoucherRef:
        """Derive the reference from a saved document.

        The type is the model's class name — ``SalesInvoice``, not
        ``sales.SalesInvoice`` — which is what makes a ledger listing readable
        without a lookup table.
        """
        if voucher is None:
            raise InvalidPosting("A ledger posting needs a voucher; got None.")

        pk = getattr(voucher, "pk", None)
        if pk is None:
            raise InvalidPosting(
                f"{type(voucher).__name__} has no primary key yet. Save the document inside "
                f"the same atomic block before posting its entries."
            )
        _assert_positive_id(pk, f"{type(voucher).__name__} primary key")

        code = getattr(voucher, "code", None)
        if not code:
            raise InvalidPosting(
                f"{type(voucher).__name__} pk={pk} has no code. Document codes come from "
                f"apps.core.services.get_next_code and are never built by hand."
            )

        return cls(type=type(voucher).__name__, id=pk, code=str(code))

    def __str__(self) -> str:
        return f"{self.type} {self.code}"
