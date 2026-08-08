"""
The chart of accounts: the tree rules, and the default chart the seed installs.

An account's ``type`` is arithmetic, not a label — it decides whether a debit
raises or lowers the account — and its position in the tree decides what gets
totalled into what. Both are things that go wrong silently, so both are tested
here rather than trusted.
"""

import datetime as dt

import pytest

from apps.accounting.chart import DEFAULT_CHART, seed_chart_of_accounts
from apps.accounting.enums import (
    CREDIT_NORMAL_TYPES,
    DEBIT_NORMAL_TYPES,
    AccountType,
    account_sign,
    party_sign,
)
from apps.accounting.exceptions import (
    GroupAccountPosting,
    InactiveAccount,
    InvalidAccount,
)
from apps.accounting.models import Account, LedgerEntry

pytestmark = pytest.mark.django_db

TODAY = dt.date(2026, 4, 1)


def make_account(code, name="Test", type_=AccountType.EXPENSE, parent=None, is_group=False):
    return Account.objects.create(
        code=code, name=name, type=type_, parent=parent, is_group=is_group
    )


class TestSeededChart:
    """The default chart, as installed by accounting migration 0002."""

    @pytest.fixture(autouse=True)
    def _seeded(self, accounts):
        """Every test here reads the chart; the fixture is what puts it there."""

    REQUIRED = [
        "Cash",
        "Bank",
        "Accounts Receivable",
        "Inventory",
        "Accounts Payable",
        "Sales",
        "Sales Returns",
        "Cost of Goods Sold",
        "Purchase",
        "Discount Allowed",
        "Discount Received",
        "Tax Payable",
        "Expenses",
        "Owner's Equity",
        "Retained Earnings",
    ]

    def test_every_required_account_exists(self):
        present = set(Account.objects.values_list("name", flat=True))
        missing = [name for name in self.REQUIRED if name not in present]
        assert not missing, f"default chart is missing: {missing}"

    def test_expenses_is_a_group_with_children(self):
        expenses = Account.objects.get(code="5000")
        assert expenses.is_group
        assert expenses.children.exists()

    def test_the_chart_nests_more_than_one_level(self):
        """Operating Expenses is a group inside a group; subtree totalling has
        to walk further than one hop."""
        rent = Account.objects.get(code="5420")
        assert [a.code for a in rent.ancestors()] == ["5400", "5000"]

    def test_only_groups_have_children(self):
        parents = set(
            Account.objects.filter(parent__isnull=False).values_list("parent__code", flat=True)
        )
        leaves_with_children = set(
            Account.objects.filter(code__in=parents, is_group=False).values_list("code", flat=True)
        )
        assert not leaves_with_children

    def test_a_child_always_shares_its_parents_type(self):
        mismatched = [
            (child.code, child.type, child.parent.type)
            for child in Account.objects.filter(parent__isnull=False).select_related("parent")
            if child.type != child.parent.type
        ]
        assert not mismatched, f"a subtree must total to one type: {mismatched}"

    def test_every_type_has_at_least_one_postable_account(self):
        for account_type in AccountType.values:
            assert Account.objects.filter(type=account_type, is_group=False).exists(), (
                f"nothing can be posted to {account_type}"
            )

    def test_roots_are_groups(self):
        for root in Account.objects.filter(parent__isnull=True):
            assert root.is_group, f"{root.code} is a root and must be a heading"

    def test_seeding_again_creates_nothing(self):
        """The seed is additive and idempotent — migrations and the repair
        command both run it, sometimes on the same database."""
        before = Account.objects.count()

        created, existing = seed_chart_of_accounts(Account)

        assert created == 0
        assert existing == len(DEFAULT_CHART)
        assert Account.objects.count() == before

    def test_seeding_again_does_not_rewrite_a_renamed_account(self):
        """An installation renames accounts. The seed must leave them alone."""
        cash = Account.objects.get(code="1110")
        cash.name = "Cash in Hand (Main Shop)"
        cash.save()

        seed_chart_of_accounts(Account)

        cash.refresh_from_db()
        assert cash.name == "Cash in Hand (Main Shop)"

    def test_seed_restores_a_missing_account(self):
        Account.objects.filter(code="5430").delete()

        created, _ = seed_chart_of_accounts(Account)

        assert created == 1
        restored = Account.objects.get(code="5430")
        assert restored.parent.code == "5400"

    def test_the_migration_is_what_installs_it(self):
        """`migrate` has to produce a database the business can post into: the
        Windows deployment is five commands and none of them seeds anything
        (CLAUDE.md §8). This runs the migration's own function, through the
        historical-model lookup it uses."""
        import importlib

        from django.apps import apps as django_apps

        Account.objects.filter(code="5470").delete()
        migration = importlib.import_module(
            "apps.accounting.migrations.0002_seed_chart_of_accounts"
        )

        migration.forwards(django_apps, None)

        assert Account.objects.get(code="5470").parent.code == "5400"
        assert Account.objects.count() == len(DEFAULT_CHART)

    def test_unwinding_the_migration_never_deletes_an_account(self):
        """Reverse is a no-op on purpose: by the time anyone runs it there may
        be ledger entries pointing at these rows."""
        import importlib

        from django.apps import apps as django_apps

        migration = importlib.import_module(
            "apps.accounting.migrations.0002_seed_chart_of_accounts"
        )

        migration.backwards(django_apps, None)

        assert Account.objects.count() == len(DEFAULT_CHART)


class TestPostability:
    def test_a_leaf_is_postable(self, accounts):
        accounts.cash.assert_postable()
        assert accounts.cash.is_postable

    def test_a_group_is_not(self, accounts):
        with pytest.raises(GroupAccountPosting, match="is a group"):
            accounts.expenses_group.assert_postable()
        assert not accounts.expenses_group.is_postable

    def test_an_inactive_account_is_not(self, accounts):
        accounts.cash.is_active = False
        accounts.cash.save()

        with pytest.raises(InactiveAccount, match="inactive"):
            accounts.cash.assert_postable()

    def test_the_error_names_the_account(self, accounts):
        with pytest.raises(GroupAccountPosting) as exc:
            accounts.expenses_group.assert_postable()
        assert "5000" in str(exc.value)


class TestTreeIntegrity:
    def test_a_parent_must_be_a_group(self, accounts):
        with pytest.raises(InvalidAccount, match="must be a group"):
            make_account("9001", parent=accounts.rent)

    def test_a_child_must_match_its_parents_type(self, accounts):
        with pytest.raises(InvalidAccount, match="total to a single type"):
            make_account("9002", type_=AccountType.ASSET, parent=accounts.expenses_group)

    def test_an_account_cannot_be_its_own_parent(self, accounts):
        accounts.rent.parent = accounts.rent
        with pytest.raises(InvalidAccount, match="own parent"):
            accounts.rent.save()

    def test_a_cycle_is_refused(self, accounts):
        """Re-parenting a group under its own descendant would make
        ``subtree_ids`` loop forever inside a report."""
        group = accounts.operating_expenses_group
        group.parent = accounts.rent  # rent is a leaf under group
        with pytest.raises(InvalidAccount):
            group.save()

    def test_a_deeper_cycle_is_refused(self, accounts):
        child_group = make_account(
            "9003", "Sub Expenses", AccountType.EXPENSE, accounts.operating_expenses_group, True
        )
        top = accounts.expenses_group
        top.parent = child_group
        with pytest.raises(InvalidAccount, match="cycle"):
            top.save()

    def test_an_account_with_entries_cannot_become_a_group(self, accounts, ledger_voucher):
        from apps.accounting.services import post_entries

        post_entries(
            ledger_voucher,
            [
                {"account": accounts.rent, "debit_paisa": 25000},
                {"account": accounts.cash, "credit_paisa": 25000},
            ],
            TODAY,
        )

        accounts.rent.is_group = True
        with pytest.raises(InvalidAccount, match="already has ledger entries"):
            accounts.rent.save()

    def test_a_group_with_children_cannot_become_a_leaf(self, accounts):
        accounts.operating_expenses_group.is_group = False
        with pytest.raises(InvalidAccount, match="has children"):
            accounts.operating_expenses_group.save()

    def test_an_empty_group_may_become_a_leaf(self, accounts):
        """The guard is about stranding rows, not about forbidding edits."""
        group = make_account(
            "9004", "Empty Group", AccountType.EXPENSE, accounts.expenses_group, True
        )

        group.is_group = False
        group.save()

        group.refresh_from_db()
        assert not group.is_group

    def test_clean_reports_the_same_rules_as_a_validation_error(self, accounts):
        """The admin has to be able to show these as a form error, not a 500."""
        from django.core.exceptions import ValidationError

        account = Account(
            code="9005", name="Bad", type=AccountType.ASSET, parent=accounts.expenses_group
        )
        with pytest.raises(ValidationError):
            account.clean()

    def test_an_account_with_entries_cannot_be_deleted(self, accounts, ledger_voucher):
        """PROTECT on the ledger's FK — history keeps its account alive."""
        from django.db.models import ProtectedError

        from apps.accounting.services import post_entries

        post_entries(
            ledger_voucher,
            [
                {"account": accounts.rent, "debit_paisa": 25000},
                {"account": accounts.cash, "credit_paisa": 25000},
            ],
            TODAY,
        )

        with pytest.raises(ProtectedError):
            accounts.rent.delete()


class TestSubtree:
    def test_a_leaf_is_its_own_subtree(self, accounts):
        assert accounts.cash.subtree_ids() == [accounts.cash.pk]

    def test_a_group_includes_its_descendants(self, accounts):
        ids = set(accounts.expenses_group.subtree_ids())

        assert accounts.expenses_group.pk in ids
        assert accounts.cogs.pk in ids
        assert accounts.operating_expenses_group.pk in ids
        assert accounts.rent.pk in ids, "must reach two levels down"
        assert accounts.cash.pk not in ids

    def test_the_subtree_is_the_whole_subtree(self, accounts):
        expected = {accounts.expenses_group.pk}
        expected |= set(
            Account.objects.filter(parent=accounts.expenses_group).values_list("pk", flat=True)
        )
        expected |= set(
            Account.objects.filter(parent=accounts.operating_expenses_group).values_list(
                "pk", flat=True
            )
        )
        assert set(accounts.expenses_group.subtree_ids()) == expected


class TestSignConvention:
    @pytest.mark.parametrize("account_type", sorted(DEBIT_NORMAL_TYPES))
    def test_debit_normal_types(self, account_type):
        assert account_sign(account_type) == 1

    @pytest.mark.parametrize("account_type", sorted(CREDIT_NORMAL_TYPES))
    def test_credit_normal_types(self, account_type):
        assert account_sign(account_type) == -1

    def test_every_type_has_a_sign(self):
        for account_type in AccountType.values:
            assert account_sign(account_type) in (1, -1)

    def test_an_unknown_type_raises_rather_than_guessing(self):
        with pytest.raises(ValueError, match="Unknown account type"):
            account_sign("PROFIT")

    def test_party_signs(self):
        assert party_sign("CLIENT") == 1
        assert party_sign("VENDOR") == -1
        with pytest.raises(ValueError, match="Unknown party type"):
            party_sign("SUPPLIER")

    def test_the_model_exposes_its_own_sign(self, accounts):
        assert accounts.cash.natural_sign == 1
        assert accounts.sales.natural_sign == -1


class TestModelBasics:
    def test_codes_are_unique(self, accounts):
        from django.db import IntegrityError, transaction

        with pytest.raises(IntegrityError), transaction.atomic():
            make_account("1110")

    def test_str_shows_code_and_name(self, accounts):
        assert str(accounts.cash) == "1110 — Cash"

    def test_ordering_is_by_code(self):
        codes = list(Account.objects.values_list("code", flat=True))
        assert codes == sorted(codes)

    def test_the_ledger_is_reachable_from_the_account(self, accounts, ledger_voucher):
        from apps.accounting.services import post_entries

        post_entries(
            ledger_voucher,
            [
                {"account": accounts.rent, "debit_paisa": 25000},
                {"account": accounts.cash, "credit_paisa": 25000},
            ],
            TODAY,
        )
        assert accounts.rent.entries.count() == 1
        assert LedgerEntry.objects.count() == 2
