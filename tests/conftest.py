"""Shared pytest fixtures.

model-bakery is the factory of choice; add app-specific recipes next to each
app rather than growing a global factory module here.
"""

from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.cache import cache


@pytest.fixture(autouse=True)
def _empty_cache():
    """Start and finish every test with a cold cache.

    The cache is local memory in one process (``config/settings/base.py``), so
    it outlives a test the way the database does not — and the dashboard writes
    a per-user entry into it that would otherwise be served to the next test
    under a rebuilt database. Autouse, because a stale figure that only appears
    when two tests run in one order is the worst kind of flake to find.
    """
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def user(db):
    return get_user_model().objects.create_user(
        username="operator",
        password="operator-pass",
        email="operator@example.test",
    )


def _clear_permission_cache(user):
    """Forget what Django already worked out about this user's permissions.

    ``has_perm`` memoises on the instance, so a fixture that grants a permission
    to a user something has already asked about would otherwise be ignored.
    """
    for attribute in ("_perm_cache", "_user_perm_cache", "_group_perm_cache"):
        user.__dict__.pop(attribute, None)
    return user


def grant(user, *permissions):
    """Give ``user`` named permissions: ``grant(u, "sales.post_salesinvoice")``.

    Raises on a name that does not exist, which is the point: a permission that
    is misspelled fails **open** — nobody holds it, so the view is simply
    unreachable and the test failure reads as a policy decision rather than a
    typo. See :func:`apps.accounts.permissions.assert_permissions_exist`.
    """
    from apps.accounts.permissions import split

    for permission in permissions:
        app_label, codename = split(permission)
        try:
            user.user_permissions.add(
                Permission.objects.get(content_type__app_label=app_label, codename=codename)
            )
        except Permission.DoesNotExist:
            raise LookupError(f"No such permission: {permission!r}") from None
    return _clear_permission_cache(user)


def ensure_groups():
    """Guarantee the five groups and every permission they hold.

    They are created by ``apps.accounts.migrations.0002_seed_groups`` and so are
    already in a freshly built test database. They are re-seeded here anyway,
    for exactly the reason the ``accounts`` fixture below re-seeds the chart: a
    ``TransactionTestCase`` — ``tests/test_sequences.py`` runs one, for the real
    SQLite locking — flushes every table on teardown and takes migration-loaded
    rows with it, for the rest of the session and for the next ``--reuse-db``
    run. Permissions and content types go with them, so both are rebuilt here
    before the groups that depend on them.

    Idempotent, so this guarantees the groups instead of depending on test
    ordering.
    """
    from django.apps import apps as global_apps
    from django.contrib.auth.management import create_permissions
    from django.contrib.auth.models import Group

    from apps.accounts.groups import GROUP_NAMES, seed_groups

    if Group.objects.filter(name__in=GROUP_NAMES).count() == len(GROUP_NAMES):
        return

    for app_config in global_apps.get_app_configs():
        previous = app_config.models_module
        app_config.models_module = True
        try:
            create_permissions(app_config, verbosity=0)
        finally:
            app_config.models_module = previous

    seed_groups(Group, Permission)


def join_group(user, *names):
    """Put ``user`` in one or more of the seeded groups.

    Preferring this to a pile of individual grants is what keeps a view test
    testing the view: if the Operator group stops being able to post a bill, the
    sales entry tests should be the thing that notices.
    """
    from django.contrib.auth.models import Group

    ensure_groups()
    for name in names:
        user.groups.add(Group.objects.get(name=name))
    return _clear_permission_cache(user)


def grant_lifecycle(user, *models):
    """``post_`` / ``cancel_`` / ``amend_`` on each document model.

    For the tests that drive a whole document through its lifecycle from a
    browser. The screens check each one separately (CLAUDE.md §5), so a test
    that only cancels should use :func:`grant_cancel` and find out.
    """
    return grant(
        user,
        *(
            permission
            for model in models
            for permission in (
                model.post_permission(),
                model.cancel_permission(),
                model.amend_permission(),
            )
        ),
    )


def grant_cancel(user, *models):
    """Give ``user`` the ``<app>.cancel_<model>`` permission for each model.

    Cancelling is permissioned per document type — see
    :meth:`~apps.core.models.DocumentModel.cancel_permission`. A test that drives
    a cancel *screen* needs the permission; a test that drives the *service*
    does not, because the service is trusted code and the permission is a rule
    about who may reach it from a browser.

    Returns the user, and clears the permission cache so a user who has already
    been asked ``has_perm`` sees the new grant.
    """
    return grant(user, *(model.cancel_permission() for model in models))


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
def warehouses(db):
    """The two places stock lives in the stock tests.

    Two, not one, because valuation is per ``(item, warehouse)`` and a single
    warehouse cannot prove that two of them hold the same item at different
    rates without averaging into each other.
    """
    from apps.accounting.models import Warehouse

    return SimpleNamespace(
        main=Warehouse.objects.create(code="MAIN", name="Main Godown", is_default=True),
        van=Warehouse.objects.create(code="VAN1", name="Delivery Van 1"),
    )


@pytest.fixture
def items(db):
    """A couple of items to move. The stock ledger only reads code and name."""
    from apps.masters.models import Item

    return SimpleNamespace(
        rice=Item.objects.create(code="RICE-5", name="Basmati Rice 5kg"),
        oil=Item.objects.create(code="OIL-1", name="Cooking Oil 1L"),
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
