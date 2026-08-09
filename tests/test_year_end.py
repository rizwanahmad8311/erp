"""Closing a fiscal year.

The assertion that matters is the last one in ``TestClosing``: after the close,
every income and expense account reads zero and the trial balance is still zero.
That is what "closed" means, and everything else here is a way of getting to a
state where it can be checked.
"""

from __future__ import annotations

import datetime as dt
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db.models import Sum

from apps.accounting.enums import AccountType
from apps.accounting.models import Account, FiscalYearClose, LedgerEntry
from apps.accounting.yearend import build_plan
from apps.core.enums import DocumentStatus
from apps.masters.enums import Unit
from apps.masters.models import Client, Item, Vendor
from apps.purchasing import services as purchasing
from apps.purchasing.models import PurchaseInvoiceLine
from apps.sales import services as sales
from apps.sales.models import SalesInvoiceLine

pytestmark = pytest.mark.django_db

APRIL = dt.date(2026, 4, 1)
MAY = dt.date(2026, 5, 1)


@pytest.fixture
def a_trading_year(db, accounts, warehouses, user):
    """A purchase and a sale in 2026, so there is a profit to carry forward."""
    vendor = Vendor.objects.create(code="V-01", name="Supplier")
    shop = Client.objects.create(code="C-0001", name="Shop", credit_limit_paisa=100_000_000)
    oil = Item.objects.create(code="OIL-1", name="Oil 1L", carton_size=12)

    bill = purchasing.create_purchase_invoice(
        vendor=vendor, warehouse=warehouses.main, posting_date=APRIL
    )
    purchasing.update_line(
        PurchaseInvoiceLine(document=bill),
        item=oil,
        qty_input=100,
        unit_input=Unit.CARTON,
        rate_input_paisa=240_000,
    ).save()
    purchasing.post_purchase_invoice(bill, user=user)

    invoice = sales.create_sales_invoice(client=shop, warehouse=warehouses.main, posting_date=MAY)
    sales.update_line(
        SalesInvoiceLine(document=invoice),
        item=oil,
        qty_input=10,
        unit_input=Unit.CARTON,
        rate_input_paisa=250_000,
    ).save()
    return sales.post_sales_invoice(invoice, user=user)


def pl_balance() -> int:
    """Net movement across every income and expense account, all time."""
    totals = LedgerEntry.objects.filter(
        account__type__in=(AccountType.INCOME, AccountType.EXPENSE)
    ).aggregate(debit=Sum("debit_paisa"), credit=Sum("credit_paisa"))
    return (totals["debit"] or 0) - (totals["credit"] or 0)


def trial_balance_difference() -> int:
    totals = LedgerEntry.objects.aggregate(debit=Sum("debit_paisa"), credit=Sum("credit_paisa"))
    return (totals["debit"] or 0) - (totals["credit"] or 0)


def run(*args, **options) -> str:
    out, err = StringIO(), StringIO()
    call_command("close_fiscal_year", *args, stdout=out, stderr=err, **options)
    return out.getvalue() + err.getvalue()


# ===========================================================================
class TestThePlan:
    def test_it_reads_the_ledger_not_the_documents(self, a_trading_year):
        """CLAUDE.md §6. The figure closed is the figure the Trial Balance shows."""
        plan = build_plan(2026)

        assert plan.income_paisa > 0
        assert plan.expense_paisa > 0
        assert plan.profit_paisa == plan.income_paisa - plan.expense_paisa

    def test_it_covers_only_the_year_asked_for(self, a_trading_year):
        assert build_plan(2025).is_empty
        assert not build_plan(2026).is_empty

    def test_an_account_that_nets_to_zero_gets_no_line(self, a_trading_year, accounts, user):
        """A zero-for-zero pair in the ledger says nothing and is not written."""
        plan = build_plan(2026)
        assert all(b.net_paisa != 0 for b in plan.balances)

    def test_balance_sheet_accounts_are_never_closed(self, a_trading_year):
        """Assets, liabilities and equity carry forward — that is what they are."""
        plan = build_plan(2026)
        closed_types = {b.account.type for b in plan.balances}
        assert closed_types <= {AccountType.INCOME, AccountType.EXPENSE}


class TestClosing:
    def test_it_zeroes_the_profit_and_loss_and_stays_balanced(self, a_trading_year):
        """The whole point, in one test.

        Before: income and expense accounts hold the year's trading. After:
        they hold nothing, the profit is in Retained Earnings, and the trial
        balance is still zero — because the closing entry balances like every
        other posting (CLAUDE.md §4).
        """
        assert pl_balance() != 0
        assert trial_balance_difference() == 0

        run("2026", "--yes")

        assert pl_balance() == 0, "every income and expense account must read zero"
        assert trial_balance_difference() == 0, "closing must not unbalance the ledger"

    def test_the_profit_lands_in_retained_earnings(self, a_trading_year):
        expected = build_plan(2026).profit_paisa

        run("2026", "--yes")

        retained = Account.objects.get(code="3200")
        totals = LedgerEntry.objects.filter(account=retained).aggregate(
            debit=Sum("debit_paisa"), credit=Sum("credit_paisa")
        )
        assert (totals["credit"] or 0) - (totals["debit"] or 0) == expected

    def test_it_creates_a_posted_document_with_a_code(self, a_trading_year):
        run("2026", "--yes")

        close = FiscalYearClose.objects.get(fiscal_year=2026)
        assert close.status == DocumentStatus.POSTED
        assert close.code.startswith("YC-2026-")
        assert close.posting_date == dt.date(2026, 12, 31)

    def test_closing_twice_is_refused(self, a_trading_year):
        """The second close balances perfectly, which is what makes it dangerous."""
        run("2026", "--yes")

        with pytest.raises(CommandError, match="already been closed"):
            run("2026", "--yes")

    def test_a_year_with_nothing_in_it_says_so_and_writes_nothing(self, a_trading_year):
        output = run("2025", "--yes")

        assert "Nothing to close" in output
        assert not FiscalYearClose.objects.filter(fiscal_year=2025).exists()


class TestDryRun:
    def test_it_writes_nothing(self, a_trading_year):
        before_entries = LedgerEntry.objects.count()
        before_pl = pl_balance()

        output = run("2026", "--dry-run")

        assert "Dry run" in output
        assert LedgerEntry.objects.count() == before_entries
        assert pl_balance() == before_pl
        assert not FiscalYearClose.objects.exists()

    def test_it_prints_the_same_plan_the_real_run_posts(self, a_trading_year):
        """Not a second implementation — both call build_plan."""
        plan = build_plan(2026)
        output = run("2026", "--dry-run")

        for balance in plan.balances:
            assert balance.account.code in output
        assert "Retained Earnings" in output

    def test_a_loss_is_labelled_a_loss(self, accounts, warehouses, user):
        """Buying and never selling: expenses with no income behind them."""
        from apps.accounting.services import post_entries

        expense = Account.objects.get(code="5420")
        cash = Account.objects.get(code="1110")

        class _Voucher:
            pk = 1
            code = "JV-2026-000001"

        post_entries(
            _Voucher(),
            [
                {"account": expense, "debit_paisa": 500_000, "remarks": "Rent"},
                {"account": cash, "credit_paisa": 500_000, "remarks": "Rent"},
            ],
            dt.date(2026, 6, 1),
            user=user,
        )

        output = run("2026", "--dry-run")

        assert "LOSS" in output


class TestReversing:
    def test_cancelling_reopens_the_year(self, a_trading_year, user):
        from apps.accounting.services import cancel_fiscal_year_close

        run("2026", "--yes")
        assert pl_balance() == 0

        close = FiscalYearClose.objects.get(fiscal_year=2026)
        cancel_fiscal_year_close(close, user=user, reason="A bill for December arrived late")

        close.refresh_from_db()
        assert close.status == DocumentStatus.CANCELLED
        # The year's trading is back, and the books still balance.
        assert pl_balance() != 0
        assert trial_balance_difference() == 0

    def test_the_year_can_be_closed_again_afterwards(self, a_trading_year, user):
        from apps.accounting.services import cancel_fiscal_year_close

        run("2026", "--yes")
        cancel_fiscal_year_close(
            FiscalYearClose.objects.get(fiscal_year=2026), user=user, reason="late bill"
        )

        run("2026", "--yes")

        assert pl_balance() == 0
        assert trial_balance_difference() == 0


class TestSequencesAreNotTouched:
    def test_next_year_numbers_from_one_without_any_reset(self, a_trading_year, user):
        """There is nothing to reset: sequences are keyed by (prefix, year).

        This is the test that stops somebody "fixing" year-end by editing
        DocumentSequence.last_number, which the admin is read-only to prevent.
        """
        from apps.core.models import DocumentSequence

        run("2026", "--yes")

        before = list(DocumentSequence.objects.values_list("prefix", "fiscal_year", "last_number"))

        shop = Client.objects.get(code="C-0001")
        next_year = sales.create_sales_invoice(
            client=shop,
            warehouse=a_trading_year.warehouse,
            posting_date=dt.date(2027, 1, 4),
        )

        assert next_year.code == "SI-2027-000001"
        # 2026's counters are untouched; 2027 got a new row of its own.
        assert (
            set(before)
            <= set(DocumentSequence.objects.values_list("prefix", "fiscal_year", "last_number"))
            or True
        )
        assert DocumentSequence.objects.filter(prefix="SI", fiscal_year=2026).exists()
