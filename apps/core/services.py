"""
Core services. Business logic lives in services, not in views, model ``save()``
or signals — CLAUDE.md §4.
"""

from __future__ import annotations

import re

from django.db import IntegrityError, transaction

from .exceptions import SequenceError
from .models import DocumentSequence

#: PREFIX-YYYY-NNNNNN
CODE_NUMBER_WIDTH = 6
_PREFIX_RE = re.compile(r"^[A-Z]{2,8}$")


def get_next_code(prefix: str, fiscal_year: int, *, width: int = CODE_NUMBER_WIDTH) -> str:
    """Allocate the next document code for a prefix and fiscal year.

        get_next_code("SI", 2026)  ->  "SI-2026-000001"
        get_next_code("SI", 2026)  ->  "SI-2026-000002"

    Concurrency
    -----------
    The counter row is taken with ``select_for_update()`` inside
    ``transaction.atomic()``, so two simultaneous invoices cannot read the same
    ``last_number`` and collide.

    Note what actually does the work on each backend, because it is not the same
    thing:

    * **SQLite** — ``select_for_update()`` is a no-op; SQLite has no row locks
      and Django simply omits the ``FOR UPDATE`` clause. Serialisation comes
      from ``transaction_mode: IMMEDIATE`` in settings, which takes the
      database-wide write lock at ``BEGIN``, plus the 20s ``timeout`` that makes
      a second writer wait rather than fail. This is what production runs on.
    * **A row-locking backend** — ``select_for_update()`` is what serialises,
      and the transaction stays narrow.

    Keeping both means the code is correct on the database we run today and on
    one we might move to, without a rewrite. The unique constraint on
    ``(prefix, fiscal_year)`` and on the document's ``code`` is the backstop
    under either.

    Call this **inside** the same ``atomic()`` block that saves the document, so
    a failed save does not burn a number. Numbers are not reused: a gap in the
    sequence is normal and is not a reason to renumber anything.
    """
    prefix = (prefix or "").strip().upper()
    if not _PREFIX_RE.match(prefix):
        raise SequenceError(f"Invalid document prefix {prefix!r}: expected 2-8 letters, A-Z.")
    if not isinstance(fiscal_year, int) or isinstance(fiscal_year, bool):
        raise SequenceError(f"Fiscal year must be an int, got {type(fiscal_year).__name__}.")
    if not 1900 <= fiscal_year <= 9999:
        raise SequenceError(f"Fiscal year {fiscal_year} is out of range.")

    with transaction.atomic():
        sequence = _locked_sequence(prefix, fiscal_year)
        sequence.last_number += 1
        sequence.save(update_fields=["last_number", "updated_at"])
        number = sequence.last_number

    return f"{prefix}-{fiscal_year}-{number:0{width}d}"


def _locked_sequence(prefix: str, fiscal_year: int) -> DocumentSequence:
    """Fetch the counter row for update, creating it on first use.

    The create is wrapped in its own ``atomic()`` block so that losing the race
    to another writer raises ``IntegrityError`` against a savepoint rather than
    poisoning the caller's transaction — that matters on backends where a failed
    statement aborts the whole block.
    """
    locked = DocumentSequence.objects.select_for_update()

    sequence = locked.filter(prefix=prefix, fiscal_year=fiscal_year).first()
    if sequence is not None:
        return sequence

    try:
        with transaction.atomic():
            DocumentSequence.objects.create(prefix=prefix, fiscal_year=fiscal_year, last_number=0)
    except IntegrityError:
        pass  # someone else created it first; fall through and lock theirs

    sequence = locked.filter(prefix=prefix, fiscal_year=fiscal_year).first()
    if sequence is None:  # pragma: no cover - only reachable if the row vanished
        raise SequenceError(f"Could not allocate a sequence for {prefix}-{fiscal_year}.")
    return sequence


def peek_next_code(prefix: str, fiscal_year: int, *, width: int = CODE_NUMBER_WIDTH) -> str:
    """The code ``get_next_code`` *would* return, without consuming it.

    For previewing a number in a form. It is a guess, not a reservation — do not
    save it. Two users peeking at once see the same value.
    """
    prefix = (prefix or "").strip().upper()
    sequence = DocumentSequence.objects.filter(prefix=prefix, fiscal_year=fiscal_year).first()
    number = (sequence.last_number if sequence else 0) + 1
    return f"{prefix}-{fiscal_year}-{number:0{width}d}"
