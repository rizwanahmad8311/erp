"""Payments invariant violations.

Like the core, accounting, purchasing and sales exceptions these are **not**
``ValidationError``: they mean the posting machinery was asked to do something
that would corrupt the ledger, not that an operator mistyped a field. They
should reach a 500 and a log line — except where a view deliberately catches one
to show it, which is what :class:`OverAllocated` is for.

Everything here inherits :class:`apps.core.exceptions.CoreError`, so a caller
that wants to catch "any broken invariant" can do so with one ``except``.
"""

from apps.core.exceptions import CoreError
from apps.core.money import fmt


class PaymentsError(CoreError):
    """Base class for every invariant violation raised by apps.payments."""


class InvalidPayment(PaymentsError):
    """Raised when the *shape* of a payment is wrong.

    A zero amount, a cheque with no number, a cheque number on a cash receipt,
    an unknown party. Distinct from :class:`OverAllocated`, which is about what
    the money was applied to rather than about the payment itself.
    """


class OverAllocated(PaymentsError):
    """Raised when money is applied to more than it can cover.

    Two rules, one exception, because from the operator's side they are the same
    mistake — a number typed into the wrong box:

    * a payment's allocations may not exceed the payment;
    * a document may not have more allocated against it than it is owed.

    The **unallocated** remainder is not an error. Money arrives before anybody
    has decided which bills it settles, and an on-account balance is a normal,
    visible state — see :attr:`~apps.payments.models.Payment.unallocated_paisa`.
    What is refused is inventing money that is not there.

    The figures are attributes as well as message text so a view can lay them
    out beside the offending row instead of parsing the sentence back apart.
    """

    def __init__(self, *, subject: str, limit_paisa: int, requested_paisa: int, hint: str = ""):
        self.subject = subject
        self.limit_paisa = limit_paisa
        self.requested_paisa = requested_paisa
        self.excess_paisa = requested_paisa - limit_paisa
        message = (
            f"{subject} can take {fmt(limit_paisa)} but {fmt(requested_paisa)} was allocated "
            f"— {fmt(self.excess_paisa)} too much."
        )
        super().__init__(f"{message} {hint}".strip())


class NotAllocatable(PaymentsError):
    """Raised when money is applied to something it cannot settle.

    A draft or cancelled invoice, a document belonging to a different party, or
    a document type that is not on the allocatable register at all. Each of
    those would produce a link that no report could make sense of: money against
    a bill that does not exist, or against somebody else's.
    """


class ChequeStateError(PaymentsError):
    """Raised when a cheque is asked to do something it cannot do next.

    Clearing a cash receipt, clearing the same cheque twice, bouncing one that
    has already cleared, or settling a cheque on a payment that was never
    posted. Every one of them would put a second set of entries against money
    that has already moved once.
    """
