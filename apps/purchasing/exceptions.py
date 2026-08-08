"""Purchasing invariant violations.

Like the core and accounting exceptions these are **not** ``ValidationError``:
they mean the posting machinery was asked to do something that would corrupt the
ledger, not that an operator mistyped a field. They should reach a 500 and a log
line — except where a view deliberately catches one to show it, which is what
:class:`PaymentAllocated` is for.

Everything here inherits :class:`apps.core.exceptions.CoreError`, so a caller
that wants to catch "any broken invariant" can do so with one ``except``.
"""

from apps.core.exceptions import CoreError

# Raised by apps.masters.pricing.compute_line, which purchasing calls for every
# line. Re-exported so `from apps.purchasing.exceptions import InvalidLine`
# keeps working and callers do not have to know where the arithmetic moved to.
from apps.masters.exceptions import InvalidLine  # noqa: F401


class PurchasingError(CoreError):
    """Base class for every invariant violation raised by apps.purchasing."""


class EmptyDocument(PurchasingError):
    """Raised when a purchase document with no lines is posted.

    A document that moves no goods and no money should not reach either ledger.
    Posting one would put a code and a vendor into the system with nothing
    behind them, and it would sit in the payables list forever at zero.
    """


class PaymentAllocated(PurchasingError):
    """Raised when a document with money allocated against it is cancelled.

    Cancelling writes reversing rows for everything the document posted. The
    payment is a **different** document with its own rows, and reversing this
    one would leave that payment sitting against a supplier balance that no
    longer has an invoice under it — money paid against nothing.

    The message names the payments, because "unallocate the payment first" is
    only actionable if you can see which payment.

    Carries the payment references as an attribute as well as in the message so
    a view can link to them rather than re-deriving the list.
    """

    def __init__(self, message: str, *, payments=()):
        self.payments = list(payments)
        super().__init__(message)
