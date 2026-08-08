"""Shared pytest fixtures.

model-bakery is the factory of choice; add app-specific recipes next to each
app rather than growing a global factory module here.
"""

from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model


@pytest.fixture
def user(db):
    return get_user_model().objects.create_user(
        username="operator",
        password="operator-pass",
        email="operator@example.test",
    )


@pytest.fixture
def accounts(db):
    """Named handles onto the default chart of accounts.

    Not a factory: tests read the real chart on purpose — a posting written as
    "debit 1130 Accounts Receivable, credit 4100 Sales" is the posting the sales
    app will actually make.

    The chart is seeded by ``accounting`` migration 0002, so it is already in a
    freshly built test database. It is re-seeded here anyway, because a
    ``TransactionTestCase`` — tests/test_sequences.py runs one for the real
    SQLite locking — flushes every table on teardown and takes migration-loaded
    rows with it, for the rest of the session and for the next ``--reuse-db``
    run. Re-seeding is idempotent, so this guarantees the chart instead of
    depending on test ordering.
    """
    from apps.accounting import chart as coa
    from apps.accounting.chart import seed_chart_of_accounts
    from apps.accounting.models import Account

    seed_chart_of_accounts(Account)
    by_code = {account.code: account for account in Account.objects.all()}
    return SimpleNamespace(
        by_code=by_code,
        cash=by_code[coa.CASH],
        bank=by_code[coa.BANK],
        receivable=by_code[coa.ACCOUNTS_RECEIVABLE],
        inventory=by_code[coa.INVENTORY],
        payable=by_code[coa.ACCOUNTS_PAYABLE],
        tax_payable=by_code[coa.TAX_PAYABLE],
        sales=by_code[coa.SALES],
        sales_returns=by_code[coa.SALES_RETURNS],
        discount_allowed=by_code[coa.DISCOUNT_ALLOWED],
        discount_received=by_code[coa.DISCOUNT_RECEIVED],
        cogs=by_code[coa.COST_OF_GOODS_SOLD],
        purchase=by_code[coa.PURCHASE],
        owners_equity=by_code[coa.OWNERS_EQUITY],
        # Groups, for the subtree-aggregation tests.
        assets_group=by_code["1000"],
        income_group=by_code["4000"],
        expenses_group=by_code["5000"],
        operating_expenses_group=by_code["5400"],
        rent=by_code["5420"],
        utilities=by_code["5430"],
    )


@pytest.fixture
def ledger_voucher(db):
    """A saved document to post ledger entries against.

    ``tests.testapp.SampleDocument`` stands in for the sales invoice that does
    not exist yet: the ledger only ever asks a voucher for its ``pk`` and its
    ``code``, which is exactly the point of the soft link.
    """
    from tests.testapp.models import SampleDocument

    return SampleDocument.objects.create(code="SI-2026-000001", party_name="Ali Traders")


@pytest.fixture
def admin_client_logged_in(client, django_user_model, db):
    admin = django_user_model.objects.create_superuser(
        username="admin", password="admin-pass", email="admin@example.test"
    )
    client.force_login(admin)
    return client
