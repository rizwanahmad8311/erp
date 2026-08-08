"""
Account and party enumerations, and the one place the sign convention lives.

Every balance in this system is derived by aggregating :class:`LedgerEntry`
(CLAUDE.md §6). Aggregation gives you a debit total and a credit total; turning
those two numbers into "the balance" needs a convention, and a convention that
is written down twice is a convention that will disagree with itself. It is
written down here, once.
"""

from django.db import models


class AccountType(models.TextChoices):
    """The five roots every chart of accounts is built from.

    The type is what decides whether a debit increases or decreases the
    account, so it is not a label — it is arithmetic. Changing an account's type
    after it has entries silently re-signs its whole history.
    """

    ASSET = "ASSET", "Asset"
    LIABILITY = "LIABILITY", "Liability"
    EQUITY = "EQUITY", "Equity"
    INCOME = "INCOME", "Income"
    EXPENSE = "EXPENSE", "Expense"


class PartyType(models.TextChoices):
    """Who the other side of a receivable or payable line is.

    A ledger row's party is a **soft link** — a type and an integer id, with no
    foreign key. The ledger must outlive the master record: a client who is
    deleted or merged in ten years' time cannot be allowed to take their
    invoices' ledger rows with them, and a ``PROTECT`` FK would make deleting
    the client impossible rather than harmless.
    """

    CLIENT = "CLIENT", "Client"
    VENDOR = "VENDOR", "Vendor"


#: Types whose normal balance is a DEBIT: what you own, what you spent.
DEBIT_NORMAL_TYPES = frozenset({AccountType.ASSET, AccountType.EXPENSE})

#: Types whose normal balance is a CREDIT: what you owe, what you are worth,
#: what you earned.
CREDIT_NORMAL_TYPES = frozenset({AccountType.LIABILITY, AccountType.EQUITY, AccountType.INCOME})


def account_sign(account_type: str) -> int:
    """``+1`` if the type is debit-normal, ``-1`` if it is credit-normal.

    Used as ``sign * (debits - credits)`` so that a positive balance always
    means "this account holds what you would expect it to hold": a positive Cash
    balance is money you have, a positive Accounts Payable balance is money you
    owe.

    A contra account — Sales Returns sits under INCOME but is written up with
    debits — therefore reports a *negative* balance. That is deliberate: it nets
    against its siblings when the group is totalled, which is exactly what a
    contra account is for.
    """
    if account_type in DEBIT_NORMAL_TYPES:
        return 1
    if account_type in CREDIT_NORMAL_TYPES:
        return -1
    raise ValueError(f"Unknown account type {account_type!r}")


def party_sign(party_type: str) -> int:
    """``+1`` for a client, ``-1`` for a vendor.

    Same rule as :func:`account_sign`, applied to the party rather than the
    account, so a positive balance always reads as "the normal direction of
    business":

    * ``CLIENT``  — receivable, debit-normal. Positive means *they owe us*.
    * ``VENDOR``  — payable, credit-normal. Positive means *we owe them*.

    Without this, a recovery report would show every vendor as a negative
    number and someone would eventually "fix" it in the wrong place.
    """
    if party_type == PartyType.CLIENT:
        return 1
    if party_type == PartyType.VENDOR:
        return -1
    raise ValueError(f"Unknown party type {party_type!r}")
