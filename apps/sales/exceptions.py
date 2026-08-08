"""Sales invariant violations.

Like the core, accounting and purchasing exceptions these are **not**
``ValidationError``: they mean the posting machinery was asked to do something
that would corrupt the ledger, not that an operator mistyped a field. They
should reach a 500 and a log line — except where a view deliberately catches one
to show it, which is what :class:`CreditLimitExceeded` is for.

Everything here inherits :class:`apps.core.exceptions.CoreError`, so a caller
that wants to catch "any broken invariant" can do so with one ``except``.
"""

from apps.core.exceptions import CoreError
from apps.core.money import fmt

# Raised by apps.masters.pricing.compute_line, which sales calls for every line.
# Re-exported so this module is the one import a sales caller needs.
from apps.masters.exceptions import InvalidLine  # noqa: F401


class SalesError(CoreError):
    """Base class for every invariant violation raised by apps.sales."""


class EmptyDocument(SalesError):
    """Raised when a sales document with no lines is posted.

    A document that moves no goods and no money should not reach either ledger.
    Posting one would put a code and a client into the system with nothing
    behind them, and it would sit in the receivables list forever at zero.
    """


class CreditLimitExceeded(SalesError):
    """Raised when posting an invoice would take a client past their limit.

    The one exception here that an operator is *meant* to see. It is a business
    decision, not a bug, so the message carries the three numbers that decision
    needs — the limit, what they already owe, and how far over this invoice puts
    them — and the view renders it rather than logging it.

    Someone holding ``sales.override_credit_limit`` can post anyway. Everyone
    else has to get the invoice authorised, which is the point of the limit.

    The figures are attributes as well as message text so a view can lay them
    out in a table instead of parsing the sentence back apart.
    """

    def __init__(self, *, client, limit_paisa: int, balance_paisa: int, total_paisa: int):
        self.client = client
        self.limit_paisa = limit_paisa
        self.balance_paisa = balance_paisa
        self.total_paisa = total_paisa
        self.would_owe_paisa = balance_paisa + total_paisa
        self.overage_paisa = self.would_owe_paisa - limit_paisa
        super().__init__(
            f"{client.name} ({client.code}) is over their credit limit. "
            f"Limit {fmt(limit_paisa)}; currently owes {fmt(balance_paisa)}; "
            f"this invoice is {fmt(total_paisa)}, which would take them to "
            f"{fmt(self.would_owe_paisa)} — {fmt(self.overage_paisa)} over. "
            f"Raise the limit, take a payment first, or have someone with the "
            f"'override credit limit' permission post it."
        )


class ReturnExceedsInvoice(SalesError):
    """Raised when a credit note sends back more than the invoice sold.

    Only checked when the return names an original invoice. An unlinked credit
    note has nothing to check against — the stock ledger is the only thing that
    can refuse it, and it will not, because goods coming back are a receipt.
    """
