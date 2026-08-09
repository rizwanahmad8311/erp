"""Closing a fiscal year.

What closing does, in one sentence: every income and expense account is brought
to zero by posting its balance to Retained Earnings, so the next year starts
from nothing and the balance sheet carries the profit forward.

Three decisions worth stating, because each of them is the sort of thing that
looks arbitrary a year later:

**It is a document, not a script.** ``FiscalYearClose`` inherits
:class:`~apps.core.models.DocumentModel` like everything else that writes to the
ledger (CLAUDE.md §5). That is not ceremony — it means the close has a code, a
status, a posted-by and a posted-at, that it can be cancelled with a reversing
entry like any other posting, and that ``tests/test_lifecycle.py::TestTheContract``
holds it to the same contract as an invoice. A closing entry that could not be
reversed would be the one posting in the system with no way back.

**The balances come from the ledger, never from a stored total** (§6). The close
aggregates ``LedgerEntry`` over the year exactly as the Trial Balance does, so
the figure it carries forward and the figure the accountant checked are the same
figure by construction.

**Nothing is deleted and no sequence is rewound.** Resetting document numbering
means the *next* year starts at 1, which it already does — sequences are keyed
by ``(prefix, fiscal_year)`` (§5). So there is nothing to reset, and this module
deliberately does not touch ``DocumentSequence``: editing ``last_number`` by hand
is the one thing the admin is read-only to prevent.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from django.db.models import Sum

from apps.accounting.enums import AccountType
from apps.accounting.models import Account, LedgerEntry
from apps.core.money import Money

#: Where the year's profit or loss lands.
RETAINED_EARNINGS = "3200"

#: The two types that are closed. Assets, liabilities and equity carry forward —
#: that is the difference between a balance sheet account and a P&L one.
CLOSING_TYPES = (AccountType.INCOME, AccountType.EXPENSE)


@dataclass(frozen=True)
class AccountBalance:
    """One account's net movement over the year being closed."""

    account: Account
    debit_paisa: int
    credit_paisa: int

    @property
    def net_paisa(self) -> int:
        """Debits minus credits. Positive for an expense, negative for income."""
        return self.debit_paisa - self.credit_paisa


@dataclass(frozen=True)
class ClosingPlan:
    """Everything the close would do, before it does any of it.

    This is what ``--dry-run`` prints and what the real run posts, so the two
    cannot disagree — the dry run is not a second implementation of the close,
    it is the same plan with the write skipped.
    """

    fiscal_year: int
    period_from: dt.date
    period_to: dt.date
    balances: tuple[AccountBalance, ...]
    retained_earnings: Account

    @property
    def income_paisa(self) -> int:
        return -sum(b.net_paisa for b in self.balances if b.account.type == AccountType.INCOME)

    @property
    def expense_paisa(self) -> int:
        return sum(b.net_paisa for b in self.balances if b.account.type == AccountType.EXPENSE)

    @property
    def profit_paisa(self) -> int:
        """Income less expenses. Negative is a loss, and that is legal."""
        return self.income_paisa - self.expense_paisa

    @property
    def is_empty(self) -> bool:
        return not self.balances


def period_for(fiscal_year: int) -> tuple[dt.date, dt.date]:
    """The calendar year, matching ``fiscal_year_of`` in every service.

    One place, so that the day this installation gets a real April-to-March
    policy there is one function to change rather than five.
    """
    return dt.date(fiscal_year, 1, 1), dt.date(fiscal_year, 12, 31)


def build_plan(fiscal_year: int) -> ClosingPlan:
    """Aggregate the year's income and expense accounts. Writes nothing.

    Cancelled documents are already netted to zero by their reversing entries
    (§3), so they are included and contribute nothing — filtering them out would
    be a second, different definition of "the year's figures" from the one the
    Trial Balance uses.
    """
    period_from, period_to = period_for(fiscal_year)

    rows = (
        LedgerEntry.objects.filter(
            posting_date__gte=period_from,
            posting_date__lte=period_to,
            account__type__in=CLOSING_TYPES,
        )
        .values("account_id")
        .annotate(debit=Sum("debit_paisa"), credit=Sum("credit_paisa"))
        .order_by("account_id")
    )
    accounts = {a.pk: a for a in Account.objects.filter(pk__in=[r["account_id"] for r in rows])}

    balances = tuple(
        AccountBalance(
            account=accounts[row["account_id"]],
            debit_paisa=row["debit"] or 0,
            credit_paisa=row["credit"] or 0,
        )
        for row in rows
        # An account whose debits and credits cancel exactly needs no closing
        # entry, and posting a zero-for-zero pair would put a row in the ledger
        # that says nothing.
        if (row["debit"] or 0) != (row["credit"] or 0)
    )

    return ClosingPlan(
        fiscal_year=fiscal_year,
        period_from=period_from,
        period_to=period_to,
        balances=balances,
        retained_earnings=Account.objects.get(code=RETAINED_EARNINGS),
    )


def gl_lines_for(plan: ClosingPlan) -> list[dict]:
    """The closing entries: every P&L account to zero, the rest to Retained Earnings.

    Each account is posted **against its own net**, on the opposite side, which
    is what brings it to zero. Retained Earnings takes the balancing figure —
    one line, not one per account, because the accountant reading the ledger
    wants "2026 result" as a single number.
    """
    lines: list[dict] = []
    for balance in plan.balances:
        net = balance.net_paisa
        lines.append(
            {
                "account": balance.account,
                # A positive net is a debit balance (an expense), so it is
                # cleared by crediting it.
                "debit_paisa": -net if net < 0 else 0,
                "credit_paisa": net if net > 0 else 0,
                "remarks": f"Closing {plan.fiscal_year}",
            }
        )

    profit = plan.profit_paisa
    if profit:
        lines.append(
            {
                "account": plan.retained_earnings,
                # A profit increases equity, which is a credit.
                "debit_paisa": -profit if profit < 0 else 0,
                "credit_paisa": profit if profit > 0 else 0,
                "remarks": f"{plan.fiscal_year} {'profit' if profit > 0 else 'loss'}",
            }
        )

    total_debit = sum(line["debit_paisa"] for line in lines)
    total_credit = sum(line["credit_paisa"] for line in lines)
    if total_debit != total_credit:  # pragma: no cover - arithmetic guard
        raise AssertionError(
            f"closing entries do not balance: {total_debit} vs {total_credit}. "
            f"This is a bug in gl_lines_for, not in the data."
        )
    return lines


def money(paisa: int) -> Money:
    """Small helper so the command formats through the one money path."""
    return Money(paisa)


__all__ = [
    "CLOSING_TYPES",
    "RETAINED_EARNINGS",
    "AccountBalance",
    "ClosingPlan",
    "build_plan",
    "gl_lines_for",
    "period_for",
]
