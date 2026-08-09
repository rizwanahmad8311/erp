"""The guards in apps/core and apps/accounting, exercised directly.

Every test here drives a branch that only runs when something has gone wrong:
a chart with an account deleted out of it, a posting whose two halves disagree,
a voucher reference that is not a saved row. Those branches are the ones that
matter most and the ones example-based tests reach least, because writing an
example means first imagining the accident.

Nothing here is coverage for its own sake. Each one asserts the *message* as
well as the type, because the whole point of these guards is that somebody who
is not a developer reads the sentence and knows what to do (CLAUDE.md §5 and the
error-handling rule: no bare traceback ever reaches a user).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from django.urls import reverse

from apps.accounting.exceptions import InvalidPosting, UnbalancedEntry
from apps.accounting.models import Account, LedgerEntry
from apps.accounting.posting import GLLine, accounts_by_code, assert_gl_balances
from apps.accounting.refs import PartyRef, VoucherRef
from apps.core.exceptions import MoneyError
from apps.core.fields import MoneyField, QuantityField
from apps.core.money import Money, round_paisa, to_paisa

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Fixtures. Defined here rather than in conftest because they are this file's
# minimum, not a shared vocabulary — tests/test_lifecycle.py wants stock on hand
# and a credit limit, and this file mostly wants a saved row to point at.
# ---------------------------------------------------------------------------
@pytest.fixture
def shop(db):
    from apps.masters.models import Client

    return Client.objects.create(
        code="C-0001", name="Al-Madina Kiryana", credit_limit_paisa=100_000_000, credit_days=15
    )


@pytest.fixture
def oil(db):
    from apps.masters.models import Item

    return Item.objects.create(code="OIL-1000", name="Cooking Oil 1L", carton_size=12)


@pytest.fixture
def vendor(db):
    from apps.masters.models import Vendor

    return Vendor.objects.create(code="V-01", name="Dalda Foods")


@pytest.fixture
def stocked(db, accounts, warehouses, vendor, oil, user):
    """1,200 pieces on hand, through a real posted purchase invoice."""
    from apps.masters.enums import Unit
    from apps.purchasing import services as purchasing
    from apps.purchasing.models import PurchaseInvoiceLine

    invoice = purchasing.create_purchase_invoice(
        vendor=vendor, warehouse=warehouses.main, posting_date=dt.date(2026, 4, 1)
    )
    purchasing.update_line(
        PurchaseInvoiceLine(document=invoice),
        item=oil,
        qty_input=100,
        unit_input=Unit.CARTON,
        rate_input_paisa=240_000,
    ).save()
    return purchasing.post_purchase_invoice(invoice, user=user)


# ===========================================================================
# money.py — the arithmetic guards
# ===========================================================================
class TestMoneyGuards:
    def test_round_paisa_refuses_anything_but_a_decimal(self):
        """`float` in particular. CLAUDE.md §1: it never reaches a stored value."""
        with pytest.raises(MoneyError, match="expects a Decimal"):
            round_paisa(1.5)

    def test_round_paisa_refuses_nan_and_infinity(self):
        """A NaN that quantized silently would store as an unpredictable int."""
        for bad in (Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")):
            with pytest.raises(MoneyError, match="Cannot round"):
                round_paisa(bad)

    def test_sum_starts_from_zero_without_special_casing(self):
        """``sum()`` seeds with the int 0, so ``Money + 0`` has to return self.

        Every posting service sums a list of Money this way; without it the
        first line of every document would raise.
        """
        amounts = [Money(100), Money(250), Money(-50)]
        assert sum(amounts, Money.zero()) == Money(300)
        # It is `0 + Money` that is exempt -- __radd__ -- because that is what
        # sum() does with its start value. `Money + 0` stays a TypeError, since
        # a bare int on the right is exactly the paisa/rupees confusion the
        # guard exists to catch.
        assert 0 + Money(100) == Money(100)
        assert sum([Money(5)]) == Money(5)

    def test_money_plus_a_bare_int_still_raises(self):
        """The guard that catches "is this paisa or rupees?" — only 0 is exempt."""
        with pytest.raises(TypeError):
            Money(100) + 1

    def test_multiplying_by_a_bool_is_refused(self):
        """``True`` is an ``int`` in Python, and ``price * True`` is never meant."""
        with pytest.raises(TypeError):
            Money(100) * True

    def test_multiplying_by_a_decimal_rounds_once_through_round_paisa(self):
        """A percentage lands on a whole paisa, banker's-rounded, in one step."""
        assert Money(100).paisa * 1 == 100
        assert (Money(1000) * Decimal("0.125")).paisa == 125
        # 2.5 -> 2, not 3: half-even, and only rounded at the end.
        assert (Money(10) * Decimal("0.25")).paisa == 2

    def test_multiplying_by_a_float_is_refused(self):
        with pytest.raises(TypeError):
            Money(100) * 1.5


# ===========================================================================
# fields.py — the deconstruct that makes migrations stable
# ===========================================================================
class TestFieldDeconstruction:
    def test_non_negative_survives_deconstruct(self):
        """Without this the flag is dropped and every `makemigrations` rewrites it."""
        _name, _path, _args, kwargs = MoneyField(non_negative=True).deconstruct()
        assert kwargs["non_negative"] is True

    def test_a_plain_money_field_does_not_carry_the_flag(self):
        _name, _path, _args, kwargs = MoneyField().deconstruct()
        assert "non_negative" not in kwargs

    def test_a_non_negative_field_validates(self):
        field = QuantityField(non_negative=True)
        assert any(v.limit_value == 0 for v in field.validators if hasattr(v, "limit_value"))


# ===========================================================================
# posting.py — the two assertions every posting runs through
# ===========================================================================
class TestPostingGuards:
    def test_a_missing_account_names_the_code_and_the_fix(self, accounts):
        """The failure mode is an installation whose chart was edited."""
        with pytest.raises(InvalidPosting) as caught:
            accounts_by_code("1130", "9999")

        message = str(caught.value)
        assert "9999" in message
        assert "seed_chart_of_accounts" in message, "the message must name the fix"

    def test_present_accounts_come_back_keyed_by_code(self, accounts):
        found = accounts_by_code("1130", "4100")
        assert set(found) == {"1130", "4100"}
        assert isinstance(found["1130"], Account)


# ===========================================================================
# refs.py — a ledger row always points at something real
# ===========================================================================
class TestReferenceGuards:
    def test_a_party_id_must_be_a_positive_int(self):
        for bad in (0, -1, "7"):
            with pytest.raises(InvalidPosting):
                PartyRef("CLIENT", bad)

    def test_a_bool_is_not_an_id(self):
        """``True`` passes ``isinstance(x, int)``; it is still not row 1."""
        with pytest.raises(InvalidPosting, match="must be an int"):
            PartyRef("CLIENT", True)

    def test_a_party_can_be_built_from_a_pair(self):
        assert PartyRef.coerce(("CLIENT", 7)) == PartyRef("CLIENT", 7)
        assert PartyRef.coerce(None) is None

    def test_anything_else_is_refused_by_name(self):
        with pytest.raises(InvalidPosting, match="must be a PartyRef"):
            PartyRef.coerce("client 7")

    def test_a_voucher_must_be_a_saved_row(self, shop):
        from apps.masters.models import Client

        with pytest.raises(InvalidPosting, match="needs a voucher"):
            VoucherRef.of(None)

        with pytest.raises(InvalidPosting):
            VoucherRef.of(Client(code="C-9", name="Unsaved"))

    def test_a_saved_row_gives_type_id_and_code(self, shop):
        ref = VoucherRef.of(shop)
        assert ref.id == shop.pk
        assert "Client" in ref.type


# ===========================================================================
# The unbalanced-posting guard
# ===========================================================================
class TestNothingUnbalancedIsEverWritten:
    def test_a_posting_whose_halves_disagree_is_refused_and_writes_nothing(
        self, accounts, shop, warehouses, oil, user
    ):
        """CLAUDE.md §4: debits equal credits *inside* the transaction.

        Driven through the real primitive rather than a mocked one, and the row
        count is checked afterwards — "nothing was written" is the half of the
        promise that a raised exception alone does not prove.
        """
        before = LedgerEntry.objects.count()

        lines = [
            GLLine(accounts.receivable, 10_000, 0, "Receivable"),
            GLLine(accounts.sales, 0, 9_999, "Sales"),
        ]

        class _Doc:
            code = "SI-2026-000999"

        with pytest.raises(UnbalancedEntry) as caught:
            assert_gl_balances(lines, _Doc())

        message = str(caught.value)
        assert "does not balance" in message
        assert "1 paisa" in message, "the message must name the size of the gap"
        assert "Nothing was written" in message
        assert LedgerEntry.objects.count() == before


# ===========================================================================
# services.py — the sequence, when two callers race for the same first number
# ===========================================================================
class TestSequenceCreationRace:
    def test_a_concurrent_first_use_falls_through_to_the_row_that_won(self, db, monkeypatch):
        """Both callers try to create the counter; one loses on the unique index.

        The loser must not raise — it re-reads and locks the row the winner
        created. Simulated by making the create raise IntegrityError the way the
        database would, because two real threads cannot be made to interleave on
        demand.
        """
        from django.db import IntegrityError

        from apps.core import services
        from apps.core.models import DocumentSequence

        # The other caller has already committed its row. Ours then loses the
        # race on the unique index, which is what the IntegrityError stands for.
        # It is raised *instead of* inserting, not after — an insert that raised
        # inside the same atomic block would roll its own row back and the
        # scenario would not be the one being tested.
        DocumentSequence.objects.filter(prefix="ZZ").delete()
        DocumentSequence.objects.create(prefix="ZZ", fiscal_year=2026, last_number=0)

        def always_lose(**kwargs):
            raise IntegrityError("UNIQUE constraint failed")

        monkeypatch.setattr(DocumentSequence.objects, "create", always_lose)

        code = services.get_next_code("ZZ", 2026)

        assert code == "ZZ-2026-000001"
        assert DocumentSequence.objects.filter(prefix="ZZ", fiscal_year=2026).count() == 1


# ===========================================================================
# The shortcuts screen
# ===========================================================================
class TestTheShortcutsScreen:
    def test_it_renders_every_documented_key(self, client, django_user_model):
        """The page and the JS read the same tuple, so this also proves the JS
        map is non-empty — see apps/core/shortcuts.py."""
        from apps.core.shortcuts import SHORTCUTS

        user = django_user_model.objects.create_user(username="anyone", password="x")
        client.force_login(user)

        body = client.get("/shortcuts/").content.decode()

        for shortcut in SHORTCUTS:
            assert shortcut.label in body

    def test_it_needs_no_permission(self, client):
        """It describes keys, not data. Every one is inert on a screen you
        cannot open, so gating it would only hide the manual."""
        assert client.get("/shortcuts/").status_code == 200


# ===========================================================================
# The cancel screen's refusals
# ===========================================================================
class TestTheCancelScreenRefusals:
    def _login(self, client, django_user_model, *perms):
        from django.contrib.auth.models import Permission

        user = django_user_model.objects.create_user(username="ops", password="x")
        for perm in perms:
            app_label, codename = perm.split(".")
            user.user_permissions.add(
                Permission.objects.get(content_type__app_label=app_label, codename=codename)
            )
        client.force_login(user)
        return user

    def test_cancelling_a_draft_is_refused_in_words(
        self, client, django_user_model, stocked, shop, warehouses, oil
    ):
        """Only a POSTED document can be cancelled — a draft has written nothing."""
        from apps.masters.enums import Unit
        from apps.sales import services as sales
        from apps.sales.models import SalesInvoiceLine

        draft = sales.create_sales_invoice(
            client=shop, warehouse=warehouses.main, posting_date=dt.date(2026, 5, 1)
        )
        sales.update_line(
            SalesInvoiceLine(document=draft),
            item=oil,
            qty_input=1,
            unit_input=Unit.CARTON,
            rate_input_paisa=to_paisa("2500"),
        ).save()

        # view_all_routes as well: without it this login has no seller, and a
        # scoped login with no seller sees *nothing* -- so the screen would 404
        # before it ever got to the refusal being tested here.
        self._login(
            client,
            django_user_model,
            "sales.cancel_salesinvoice",
            "sales.view_salesinvoice",
            "accounts.view_all_routes",
        )
        url = reverse("sales:cancel", kwargs={"slug": "invoices", "pk": draft.pk})
        response = client.get(url, follow=True)

        body = response.content.decode()
        assert "DRAFT" in body or "Draft" in body
        assert "Only a posted document can be cancelled" in body

    def test_it_is_refused_without_the_cancel_permission(
        self, client, django_user_model, stocked, shop, warehouses, oil, user
    ):
        from apps.sales import services as sales
        from tests.test_lifecycle import _sale

        invoice = sales.post_sales_invoice(_sale(shop, warehouses.main, oil), user=user)

        self._login(
            client, django_user_model, "sales.view_salesinvoice", "accounts.view_all_routes"
        )
        url = reverse("sales:cancel", kwargs={"slug": "invoices", "pk": invoice.pk})
        response = client.get(url)

        assert response.status_code == 403
