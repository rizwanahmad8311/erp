"""Building a document's general ledger, and checking it before it is written.

The piece every transaction app needs and none of them should own a copy of. A
purchase invoice and a sales invoice post opposite entries, but "what are the
debit and credit rows, do they balance, and does the Inventory row agree with
what the stock ledger actually moved" is the same question in both directions.

Nothing here writes anything. :func:`~apps.accounting.services.post_entries` and
:func:`~apps.accounting.services.post_stock` do the writing; this is what the
posting services build and assert with first, and what their entry screens
render as a preview. The preview and the posting coming from one function is the
point — a preview computed separately is a preview that will eventually disagree
with what lands in the ledger, which is worse than showing nothing at all.
"""

from __future__ import annotations

from typing import NamedTuple

from apps.core.money import Money

from . import chart as coa
from .exceptions import InvalidPosting, UnbalancedEntry
from .models import Account
from .refs import PartyRef


class GLLine(NamedTuple):
    """One side of a document's posting, ready to render or to post.

    ``label`` is what the row says on the preview and in the entry's remarks —
    "Goods received", "Cost of goods sold" — so a ledger listing reads as
    sentences rather than as account codes.
    """

    account: Account
    debit_paisa: int
    credit_paisa: int
    label: str

    def as_entry(self, party: PartyRef | None = None) -> dict:
        """The dict shape :func:`apps.accounting.services.post_entries` wants."""
        entry = {
            "account": self.account,
            "debit_paisa": self.debit_paisa,
            "credit_paisa": self.credit_paisa,
            "remarks": self.label,
        }
        if party is not None:
            entry["party"] = party
        return entry


def accounts_by_code(*codes: str) -> dict[str, Account]:
    """Fetch the accounts a posting needs, in one query.

    Raises with the missing codes rather than a bare ``DoesNotExist``: an
    installation whose chart has been edited is exactly when this fails, and
    "account 4400 is missing" is the sentence that fixes it.
    """
    found = {account.code: account for account in Account.objects.filter(code__in=codes)}
    missing = [code for code in codes if code not in found]
    if missing:
        raise InvalidPosting(
            f"The chart of accounts is missing {', '.join(missing)}. Run "
            f"`manage.py seed_chart_of_accounts` — it only creates what is absent."
        )
    return found


def drop_zero_lines(gl_lines) -> list[GLLine]:
    """Discard rows that move nothing.

    A document with no discount should not post a zero Discount line, and
    ``post_entries`` refuses one anyway — a zero row adds nothing to the ledger
    and hides the fact that something upstream computed zero.
    """
    return [line for line in gl_lines if line.debit_paisa or line.credit_paisa]


def assert_gl_balances(gl_lines, document) -> Money:
    """Debits == credits, to the paisa, before anything is written.

    :func:`~apps.accounting.services.post_entries` checks this too and is the
    real guarantee. This runs first so that a bug in a *document's* arithmetic
    is reported against the document the operator is looking at, rather than as
    a generic unbalanced-voucher error a layer down.

    Returns the total, which is occasionally worth having.
    """
    debits = sum((Money(line.debit_paisa) for line in gl_lines), Money.zero())
    credits = sum((Money(line.credit_paisa) for line in gl_lines), Money.zero())
    if debits != credits:
        difference = debits - credits
        raise UnbalancedEntry(
            f"{type(document).__name__} {document.code} does not balance: debits "
            f"{debits.paisa} paisa vs credits {credits.paisa} paisa — a difference of "
            f"{difference.paisa} paisa. Nothing was written."
        )
    return debits


def assert_inventory_matches_stock(gl_lines, movements, document) -> None:
    """The two ledgers must agree on what the goods were worth, to the paisa.

    The Inventory row in the general ledger and the sum of the stock rows are
    two independent computations of the same fact. If they ever disagree,
    inventory value and the balance sheet have quietly parted company and every
    report after that is wrong by the difference — which is exactly the failure
    the whole rounding design in :mod:`apps.masters.pricing` exists to prevent.

    Both sides are signed the same way and no direction argument is needed:
    stock coming in is a positive movement and a debit, stock going out is a
    negative movement and a credit.
    """
    inventory = sum(
        (
            Money(line.debit_paisa) - Money(line.credit_paisa)
            for line in gl_lines
            if line.account.code == coa.INVENTORY
        ),
        Money.zero(),
    )
    stock_value = sum((Money(movement.value_paisa) for movement in movements), Money.zero())

    if inventory != stock_value:
        raise UnbalancedEntry(
            f"{type(document).__name__} {document.code}: the general ledger moves Inventory "
            f"by {inventory.paisa} paisa but the stock ledger moved {stock_value.paisa} "
            f"paisa. The two must agree exactly. Nothing was written."
        )


__all__ = [
    "GLLine",
    "accounts_by_code",
    "assert_gl_balances",
    "assert_inventory_matches_stock",
    "drop_zero_lines",
]
