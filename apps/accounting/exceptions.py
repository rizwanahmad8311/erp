"""
Accounting invariant violations.

Like the core exceptions these are **not** ``ValidationError``: they mean the
posting machinery was asked to do something that would corrupt the ledger, not
that an operator mistyped a field. They should reach a 500 and a log line.

Everything here inherits :class:`apps.core.exceptions.CoreError`, so a caller
that wants to catch "any broken invariant" can do so with one ``except``.
"""

from apps.core.exceptions import CoreError


class AccountingError(CoreError):
    """Base class for every invariant violation raised by apps.accounting."""


class InvalidAccount(AccountingError):
    """Raised when a chart-of-accounts change would break the tree.

    A parent that is not a group, a child whose type disagrees with its parent,
    a cycle, or flipping ``is_group`` on an account that already has entries or
    children.
    """


class InvalidPosting(AccountingError):
    """Raised when the *shape* of a posting request is wrong.

    No lines, a line missing an account, an amount that is not whole paisa, both
    sides of a line filled in, a half-set party, a ``datetime`` where a
    ``date`` was required. Distinct from :class:`UnbalancedEntry`, which is
    about the totals rather than the shape.
    """


class GroupAccountPosting(InvalidPosting):
    """Raised when an entry is aimed at a group (header) account.

    Only leaf accounts receive entries. A group's balance is the sum of its
    subtree; letting it hold entries of its own means the tree no longer adds
    up and the totals silently double-count.
    """


class InactiveAccount(InvalidPosting):
    """Raised when a *new* posting targets a deactivated account.

    Reversals deliberately do not raise this — see
    :func:`apps.accounting.services.reverse_entries`. Deactivating an account
    must never make an outstanding document impossible to cancel.
    """


class InvalidWarehouse(AccountingError):
    """Raised when the warehouse list would stop having exactly one default.

    Two rows flagged ``is_default``, or none at all when something asked for
    the default. Both mean a document that does not name a warehouse would
    silently pick one — or pick nothing — and stock would land somewhere
    nobody chose.
    """


class InsufficientStock(AccountingError):
    """Raised when an issue would take an ``(item, warehouse)`` balance negative.

    Carries the numbers as attributes as well as in the message, so a view can
    render "only 40 left" against the right line instead of re-deriving it.

    Switched off by ``settings.ALLOW_NEGATIVE_STOCK`` for installations that
    genuinely invoice before the paperwork for the receipt catches up. It
    defaults to off, because negative stock silently poisons the moving average
    that every issue after it is valued at.
    """

    def __init__(self, *, item, warehouse, requested: int, available: int):
        #: The models are held for the caller's benefit; only ``code`` and
        #: ``name`` are read here, so this module stays free of model imports.
        self.item = item
        self.warehouse = warehouse
        self.requested = requested
        self.available = available
        super().__init__(
            f"Not enough stock of {item.name} ({item.code}) in warehouse {warehouse.code}: "
            f"{requested} base unit(s) requested, {available} available. Receive the stock "
            f"first, or set ALLOW_NEGATIVE_STOCK=True if this installation really does issue "
            f"before it receives."
        )


class UnbalancedEntry(AccountingError):
    """Raised when the debits and credits of a posting are not exactly equal.

    The message always states the difference in paisa, because "off by 1" and
    "off by 100000" are different bugs and the number is the fastest way to tell
    which one you are looking at.
    """


class AlreadyPosted(AccountingError):
    """Raised when a voucher that already has ledger rows is posted again.

    Double-posting is the single most expensive accident available in this
    system: the document looks right, the ledger is silently doubled, and it is
    found weeks later by a bank reconciliation.
    """


class AlreadyReversed(AccountingError):
    """Raised when a voucher has nothing left to reverse.

    Either it was never posted, or it has already been cancelled. Reversing a
    reversal would put the original amounts back and net the cancellation out
    to nothing.
    """
