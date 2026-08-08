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


class DocumentHasDependents(CoreError):
    """Raised when something else would be left dangling by a cancellation.

    Cancelling writes reversing rows for everything *this* document posted, and
    nothing else. Anything hanging off it — a payment allocated to it, a credit
    note issued against it, a cheque event recording what the bank did — is a
    **different** document with its own rows, and reversing this one alone would
    leave those rows pointing at something that is no longer in the books.

    The message names every blocker and what to do about it, because "deal with
    the other document first" is only actionable if you can see which one.

    The blockers are carried as :class:`~apps.core.lifecycle.Dependent` records
    on ``.dependents`` as well as in the message, so a screen can list them with
    links instead of parsing the sentence back apart.
    """

    def __init__(self, message: str, *, dependents=()):
        self.dependents = list(dependents)
        super().__init__(message)


class PaymentAllocated(DocumentHasDependents):
    """Raised when a document with money allocated against it is cancelled.

    Lives in core rather than in purchasing because it is not a purchasing rule:
    money is allocated to sales invoices, purchase invoices and credit notes
    alike, and each of those apps refusing with an exception of its own is how
    two screens end up wording the same refusal differently.
    """

    @property
    def payments(self):
        """The blocking payments. An alias for :attr:`dependents` that reads
        the way the purchasing screens talk about them."""
        return self.dependents


class AppendOnlyViolation(CoreError):
    """Raised on any attempt to UPDATE or DELETE an append-only row."""


class MoneyError(CoreError):
    """Raised when a value cannot be interpreted as an exact monetary amount."""


class SequenceError(CoreError):
    """Raised when a document code cannot be allocated."""
