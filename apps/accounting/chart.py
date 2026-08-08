"""
The default chart of accounts for a distribution business.

Numbering follows the usual convention, so the code sorts into report order and
the first digit tells you the type without a lookup:

    1xxx  Assets        2xxx  Liabilities   3xxx  Equity
    4xxx  Income        5xxx  Expenses

Groups are headings and hold no entries of their own; the leaves under them do.
:func:`seed_chart_of_accounts` is **additive and idempotent** — it creates what
is missing by code and never updates or deletes what is already there. A live
installation will have renamed accounts and added its own, and a seed that
"corrected" those would be a data loss bug wearing a helpful face.
"""

from __future__ import annotations

from .enums import AccountType

ASSET = AccountType.ASSET
LIABILITY = AccountType.LIABILITY
EQUITY = AccountType.EQUITY
INCOME = AccountType.INCOME
EXPENSE = AccountType.EXPENSE


def _account(code, name, type_, parent=None, group=False) -> dict:
    return {"code": code, "name": name, "type": type_, "parent": parent, "is_group": group}


#: Parents appear before their children — :func:`seed_chart_of_accounts` relies
#: on it rather than sorting, so the file reads top-down as the chart does.
DEFAULT_CHART: tuple[dict, ...] = (
    # -- 1xxx Assets --------------------------------------------------------
    _account("1000", "Assets", ASSET, group=True),
    _account("1100", "Current Assets", ASSET, parent="1000", group=True),
    _account("1110", "Cash", ASSET, parent="1100"),
    _account("1120", "Bank", ASSET, parent="1100"),
    _account("1130", "Accounts Receivable", ASSET, parent="1100"),
    _account("1140", "Inventory", ASSET, parent="1100"),
    _account("1150", "Advances to Suppliers", ASSET, parent="1100"),
    # A cheque taken from a shop is not money yet. It sits here until the bank
    # says otherwise — see apps.payments. Post-dated cheques are how this
    # business runs, so this account is routinely the second largest asset on
    # the sheet and must never be folded into Bank.
    _account("1160", "Cheques in Hand", ASSET, parent="1100"),
    # -- 2xxx Liabilities ---------------------------------------------------
    _account("2000", "Liabilities", LIABILITY, group=True),
    _account("2100", "Current Liabilities", LIABILITY, parent="2000", group=True),
    _account("2110", "Accounts Payable", LIABILITY, parent="2100"),
    _account("2120", "Tax Payable", LIABILITY, parent="2100"),
    _account("2130", "Advances from Customers", LIABILITY, parent="2100"),
    # The mirror of 1160: a cheque we have written and the supplier has not yet
    # presented. The money has left our books but not our bank, and a bank
    # reconciliation that cannot see the difference is a bank reconciliation
    # that never balances.
    _account("2140", "Cheques Issued", LIABILITY, parent="2100"),
    # -- 3xxx Equity --------------------------------------------------------
    _account("3000", "Equity", EQUITY, group=True),
    _account("3100", "Owner's Equity", EQUITY, parent="3000"),
    _account("3200", "Retained Earnings", EQUITY, parent="3000"),
    _account("3300", "Owner's Drawings", EQUITY, parent="3000"),
    # -- 4xxx Income --------------------------------------------------------
    _account("4000", "Income", INCOME, group=True),
    _account("4100", "Sales", INCOME, parent="4000"),
    # Contra-income: written up with debits, so it reports a negative balance
    # and nets against Sales when the Income group is totalled.
    _account("4200", "Sales Returns", INCOME, parent="4000"),
    _account("4300", "Discount Received", INCOME, parent="4000"),
    _account("4400", "Other Income", INCOME, parent="4000"),
    # -- 5xxx Expenses ------------------------------------------------------
    _account("5000", "Expenses", EXPENSE, group=True),
    _account("5100", "Cost of Goods Sold", EXPENSE, parent="5000"),
    # Kept alongside COGS on purpose. Stock movements post Inventory and COGS
    # (perpetual); Purchase is for buying that never becomes stock.
    _account("5200", "Purchase", EXPENSE, parent="5000"),
    _account("5300", "Discount Allowed", EXPENSE, parent="5000"),
    _account("5400", "Operating Expenses", EXPENSE, parent="5000", group=True),
    _account("5410", "Salaries & Wages", EXPENSE, parent="5400"),
    _account("5420", "Rent", EXPENSE, parent="5400"),
    _account("5430", "Utilities", EXPENSE, parent="5400"),
    _account("5440", "Fuel & Vehicle Running", EXPENSE, parent="5400"),
    _account("5450", "Freight & Carriage", EXPENSE, parent="5400"),
    _account("5460", "Repairs & Maintenance", EXPENSE, parent="5400"),
    _account("5470", "Bank Charges", EXPENSE, parent="5400"),
    _account("5480", "Miscellaneous Expenses", EXPENSE, parent="5400"),
)


#: Accounts the posting services will reach for by name. Kept here so that the
#: day a service needs "the receivable account" it looks it up by a documented
#: code rather than by a string typed into three different files.
CASH = "1110"
BANK = "1120"
ACCOUNTS_RECEIVABLE = "1130"
INVENTORY = "1140"
CHEQUES_IN_HAND = "1160"
ACCOUNTS_PAYABLE = "2110"
TAX_PAYABLE = "2120"
CHEQUES_ISSUED = "2140"
SALES = "4100"
SALES_RETURNS = "4200"
DISCOUNT_RECEIVED = "4300"
OTHER_INCOME = "4400"
COST_OF_GOODS_SOLD = "5100"
PURCHASE = "5200"
DISCOUNT_ALLOWED = "5300"
MISCELLANEOUS_EXPENSES = "5480"
OWNERS_EQUITY = "3100"
RETAINED_EARNINGS = "3200"


def seed_chart_of_accounts(account_model) -> tuple[int, int]:
    """Create any missing default accounts. Returns ``(created, already_there)``.

    ``account_model`` is passed in rather than imported so that the data
    migration can hand over its historical model — a migration that imports the
    live model breaks the day the model gains a field.

    Safe to run repeatedly: matching is by ``code``, existing rows are left
    exactly as they are, and nothing is ever deleted.
    """
    created = 0
    existing = 0
    by_code: dict[str, object] = {}

    for spec in DEFAULT_CHART:
        parent_code = spec["parent"]
        # Parents are listed first, so the cache normally has it. The database
        # lookup is the safety net for a future reordering of DEFAULT_CHART: a
        # missing parent raises DoesNotExist here rather than silently seeding
        # an orphaned account at the root of the chart.
        parent = None
        if parent_code:
            parent = by_code.get(parent_code) or account_model.objects.get(code=parent_code)

        account, was_created = account_model.objects.get_or_create(
            code=spec["code"],
            defaults={
                "name": spec["name"],
                "type": spec["type"],
                "parent": parent,
                "is_group": spec["is_group"],
                "is_active": True,
            },
        )
        by_code[spec["code"]] = account
        created += was_created
        existing += not was_created

    return created, existing
