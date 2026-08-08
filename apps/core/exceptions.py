"""Domain exceptions raised by the core primitives.

These are deliberately not Django's ValidationError: they signal a broken
invariant in the posting machinery, not bad user input on a form. They should
reach a 500 and a log line, not a field-level error message.
"""


class CoreError(Exception):
    """Base class for every invariant violation raised by apps.core."""


class DocumentImmutable(CoreError):
    """Raised when something tries to modify a POSTED or CANCELLED document."""


class IllegalTransition(CoreError):
    """Raised on a status change outside DRAFT -> POSTED -> CANCELLED."""


class AppendOnlyViolation(CoreError):
    """Raised on any attempt to UPDATE or DELETE an append-only row."""


class MoneyError(CoreError):
    """Raised when a value cannot be interpreted as an exact monetary amount."""


class SequenceError(CoreError):
    """Raised when a document code cannot be allocated."""
