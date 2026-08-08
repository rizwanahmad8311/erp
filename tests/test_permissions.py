"""Who can reach what: one class per group, and the exact URLs each may open.

The centre of this file is :class:`TestTheAccessMatrix`. Every group is signed
in and every URL in :data:`URLS` is requested, and both halves are asserted —
what opens **and what does not**. Testing only the first half is how a
permission system ends up letting everybody do everything: every test passes,
and the ones that should have failed were never written.

The matrix is declared once, as data. A URL added to :data:`URLS` without being
listed in a group's ``allowed`` set is asserted to be *refused* for that group,
so the failure mode of forgetting is a test failure rather than an open door.

Three things beyond the matrix, each of which is a real way this could leak:

* **the URL a booker guesses** — hiding another beat's shop from a list and then
  serving it to anybody who types its id is not access control
  (:class:`TestBookerRowScope`);
* **the export** — a cost column masked on the screen and written into the CSV
  is the usual leak, so it is checked in all three formats
  (:class:`TestCostPriceMasking`);
* **the sidebar** — a menu that offers a screen the click refuses is worse than
  no menu (:class:`TestTheSidebarAgreesWithTheViews`).
"""

from __future__ import annotations

import csv
import datetime as dt
import io

import pytest
from django.contrib.auth.models import Group, Permission
from django.urls import reverse

from apps.accounts import permissions as perms
from apps.accounts.access import has_access
from apps.accounts.groups import GROUP_DEFINITIONS, GROUP_NAMES, seed_groups
from apps.accounts.models import UserProfile
from apps.core.money import to_paisa
from apps.masters.enums import Unit
from apps.masters.models import Client, Item, Route, RouteSeller, Seller, Vendor
from apps.purchasing import services as purchasing
from apps.purchasing.models import PurchaseInvoiceLine
from apps.reports.registry import REPORTS, get_report
from apps.sales import services as sales
from apps.sales.models import SalesInvoiceLine
from tests.conftest import ensure_groups, join_group

pytestmark = pytest.mark.django_db

APRIL = dt.date(2026, 4, 1)
MAY = dt.date(2026, 5, 1)
TODAY = dt.date(2026, 7, 1)


# ===========================================================================
# A business with two routes, so "the other route" is a real place
# ===========================================================================
@pytest.fixture
def routes(db):
    return {
        "own": Route.objects.create(code="R-01", name="Saddar & City"),
        "other": Route.objects.create(code="R-02", name="Gulshan"),
    }


@pytest.fixture
def sellers(db, routes):
    """Two bookers, one route each — the link row-level scope is derived from."""
    ours = Seller.objects.create(code="S-01", name="Imran Qureshi")
    theirs = Seller.objects.create(code="S-02", name="Bilal Ahmed")
    RouteSeller.objects.create(route=routes["own"], seller=ours, is_primary=True)
    RouteSeller.objects.create(route=routes["other"], seller=theirs, is_primary=True)
    return {"ours": ours, "theirs": theirs}


@pytest.fixture
def shops(db, routes, sellers):
    return {
        "own": Client.objects.create(
            code="C-0001",
            name="Al-Madina Kiryana",
            route=routes["own"],
            seller=sellers["ours"],
            credit_limit_paisa=to_paisa("500000"),
            credit_days=15,
        ),
        "other": Client.objects.create(
            code="C-0002",
            name="Gulshan General Store",
            route=routes["other"],
            seller=sellers["theirs"],
            credit_limit_paisa=to_paisa("500000"),
            credit_days=15,
        ),
    }


@pytest.fixture
def oil(db):
    return Item.objects.create(
        code="OIL-1000", name="Cooking Oil 1L", carton_size=12, sale_rate_paisa=to_paisa("250")
    )


@pytest.fixture
def vendor(db):
    return Vendor.objects.create(code="V-01", name="Dalda Foods")


@pytest.fixture
def stock(db, accounts, warehouses, vendor, oil, user):
    bill = purchasing.create_purchase_invoice(
        vendor=vendor, warehouse=warehouses.main, posting_date=APRIL, vendor_bill_no="DF-1"
    )
    purchasing.update_line(
        PurchaseInvoiceLine(document=bill),
        item=oil,
        qty_input=100,
        unit_input=Unit.CARTON,
        rate_input_paisa=to_paisa("2400"),
    ).save()
    return purchasing.post_purchase_invoice(bill, user=user)


@pytest.fixture
def invoices(db, stock, shops, warehouses, oil, user):
    """One posted invoice on each route, so scope has something to hide."""
    out = {}
    for key, shop in shops.items():
        document = sales.create_sales_invoice(
            client=shop, warehouse=warehouses.main, posting_date=MAY
        )
        sales.update_line(
            SalesInvoiceLine(document=document),
            item=oil,
            qty_input=2,
            unit_input=Unit.CARTON,
            rate_input_paisa=to_paisa("3000"),
        ).save()
        out[key] = sales.post_sales_invoice(document, user=user)
    return out


def make_user(django_user_model, username: str, group: str | None = None, seller=None):
    """A login in one group, optionally tied to a booker."""
    user = django_user_model.objects.create_user(username=username, password="x", is_staff=True)
    if group:
        join_group(user, group)
    if seller is not None:
        profile = UserProfile.for_user(user)
        profile.seller = seller
        profile.save(update_fields=["seller", "updated_at"])
    return user


@pytest.fixture
def people(django_user_model, sellers, db):
    """One login per group. The Booker is tied to the seller who walks R-01."""
    ensure_groups()
    return {
        "Admin": make_user(django_user_model, "role-admin", "Admin"),
        "Accountant": make_user(django_user_model, "role-accountant", "Accountant"),
        "Operator": make_user(django_user_model, "role-operator", "Operator"),
        "Booker": make_user(django_user_model, "role-booker", "Booker", seller=sellers["ours"]),
        "Viewer": make_user(django_user_model, "role-viewer", "Viewer"),
        # Somebody who has signed in and has been put in no group at all. The
        # most common setup mistake, and it must be able to do nothing.
        "Nobody": make_user(django_user_model, "role-nobody"),
    }


# ===========================================================================
# The matrix
# ===========================================================================
def url(name: str, **kwargs) -> str:
    return reverse(name, kwargs=kwargs or None)


#: Every URL worth asserting access to, and the groups that may open it.
#: Anything a group is not listed for is asserted to be **refused** for that
#: group — so adding a URL here without listing it fails the build rather than
#: quietly opening a door.
URLS: dict[str, dict] = {
    # -- sales ---------------------------------------------------------------
    "sales list": {
        "url": lambda f: url("sales:list", slug="invoices"),
        "allowed": {"Admin", "Accountant", "Operator", "Booker", "Viewer"},
    },
    "sales entry": {
        "url": lambda f: url("sales:create", slug="invoices"),
        "allowed": {"Admin", "Accountant", "Operator"},
    },
    "credit note list": {
        "url": lambda f: url("sales:list", slug="returns"),
        "allowed": {"Admin", "Accountant", "Operator", "Viewer"},
    },
    # -- purchasing ----------------------------------------------------------
    "purchase list": {
        "url": lambda f: url("purchasing:list", slug="invoices"),
        "allowed": {"Admin", "Accountant", "Operator", "Viewer"},
    },
    "purchase entry": {
        "url": lambda f: url("purchasing:create", slug="invoices"),
        "allowed": {"Admin", "Accountant", "Operator"},
    },
    # -- payments ------------------------------------------------------------
    "recovery workspace": {
        "url": lambda f: url("payments:recovery"),
        "allowed": {"Admin", "Accountant", "Operator", "Booker", "Viewer"},
    },
    "payment list": {
        "url": lambda f: url("payments:list"),
        "allowed": {"Admin", "Accountant", "Operator", "Booker", "Viewer"},
    },
    "take a payment": {
        "url": lambda f: url("payments:create"),
        "allowed": {"Admin", "Accountant", "Booker"},
    },
    "cheque register": {
        "url": lambda f: url("payments:cheques"),
        "allowed": {"Admin", "Accountant", "Operator", "Booker", "Viewer"},
    },
    # -- reports -------------------------------------------------------------
    "report index": {
        "url": lambda f: url("reports:index"),
        "allowed": {"Admin", "Accountant", "Operator", "Booker", "Viewer"},
    },
    "route day sheet": {
        "url": lambda f: url("reports:report", slug="route-day-sheet"),
        "allowed": {"Admin", "Accountant", "Operator", "Booker", "Viewer"},
    },
    "stock balance": {
        "url": lambda f: url("reports:report", slug="stock-balance"),
        "allowed": {"Admin", "Accountant", "Operator", "Booker", "Viewer"},
    },
    "trial balance": {
        "url": lambda f: url("reports:report", slug="trial-balance"),
        "allowed": {"Admin", "Accountant"},
    },
    "profit and loss": {
        "url": lambda f: url("reports:report", slug="profit-and-loss"),
        "allowed": {"Admin", "Accountant"},
    },
    "balance sheet": {
        "url": lambda f: url("reports:report", slug="balance-sheet"),
        "allowed": {"Admin", "Accountant"},
    },
}


def _reachable(response) -> bool:
    """Whether this response means "you may open this".

    A 302 to the login screen and a 403 both mean no. A 200 means yes, and so
    does a 422 — the cancel screen answers a bad POST that way and the person
    plainly reached it.
    """
    if response.status_code in (302, 403):
        return False
    return response.status_code in (200, 422)


class TestTheAccessMatrix:
    """One test per group, asserting the exact set of URLs it can reach."""

    @pytest.mark.parametrize("group", [*GROUP_NAMES, "Nobody"])
    def test_the_group_reaches_exactly_what_it_should(
        self, group, people, client, invoices, request
    ):
        """Both halves: what opens, and what is refused.

        Asserting only the first half is how a permission system ends up
        letting everybody do everything — every test passes, and the ones that
        should have failed were never written.
        """
        client.force_login(people[group])

        opened, refused = set(), set()
        for name, spec in URLS.items():
            response = client.get(spec["url"](request))
            (opened if _reachable(response) else refused).add(name)

        expected_open = {name for name, spec in URLS.items() if group in spec["allowed"]}
        assert opened == expected_open, (
            f"{group} opened {sorted(opened - expected_open)} it should not, and was "
            f"refused {sorted(expected_open - opened)} it should reach"
        )
        assert refused == set(URLS) - expected_open

    def test_a_user_in_no_group_can_reach_nothing(self, people, client, invoices, request):
        """The most common setup mistake, and it must fail closed."""
        client.force_login(people["Nobody"])
        for name, spec in URLS.items():
            assert not _reachable(client.get(spec["url"](request))), name

    def test_signed_out_is_sent_to_the_login_screen(self, client, invoices, request):
        """Not a 403 — an anonymous visitor has something they can do about it."""
        for name, spec in URLS.items():
            response = client.get(spec["url"](request))
            assert response.status_code == 302, name
            assert "login" in response["Location"], name


class TestLifecycleActions:
    """Posting, cancelling and amending are three permissions, not one."""

    def test_an_operator_posts_but_cannot_cancel(self, people, client, invoices):
        client.force_login(people["Operator"])
        document = invoices["own"]

        assert client.get(url("sales:cancel", slug="invoices", pk=document.pk)).status_code == 403
        assert client.post(url("sales:amend", slug="invoices", pk=document.pk)).status_code == 403

    def test_an_accountant_cancels(self, people, client, invoices):
        client.force_login(people["Accountant"])
        assert (
            client.get(url("sales:cancel", slug="invoices", pk=invoices["own"].pk)).status_code
            == 200
        )

    def test_a_viewer_cannot_post(self, people, client, invoices, shops, warehouses):
        draft = sales.create_sales_invoice(
            client=shops["own"], warehouse=warehouses.main, posting_date=MAY
        )
        client.force_login(people["Viewer"])
        assert client.post(url("sales:post", slug="invoices", pk=draft.pk)).status_code == 403
        draft.refresh_from_db()
        assert draft.status == "DRAFT"

    def test_a_booker_may_post_a_receipt_and_nothing_else(self, people):
        booker = people["Booker"]
        assert has_access(booker, "payments.post_payment")
        assert not has_access(booker, "payments.cancel_payment")
        assert not has_access(booker, "sales.post_salesinvoice")


# ===========================================================================
# Row-level scope
# ===========================================================================
class TestBookerRowScope:
    """A booker sees their own routes. Typing an id does not get round it."""

    def test_the_recovery_sheet_shows_only_their_own_shops(self, people, client, invoices, shops):
        client.force_login(people["Booker"])
        body = client.get(url("payments:recovery")).content.decode()

        assert shops["own"].name in body
        assert shops["other"].name not in body, "another beat's shop is on the sheet"

    def test_guessing_the_url_of_another_routes_client_is_a_404(
        self, people, client, invoices, shops
    ):
        """The test this whole scoping layer exists for.

        Hiding a shop from a list and then serving it to anybody who types its
        id is not access control, it is decoration. A 404 rather than a 403 on
        purpose: a 403 would confirm the shop exists and that somebody else has
        it, and "nothing" is the right amount to tell a booker about another
        beat's customer.
        """
        client.force_login(people["Booker"])

        mine = client.get(url("payments:recovery-client", pk=shops["own"].pk))
        theirs = client.get(url("payments:recovery-client", pk=shops["other"].pk))

        assert mine.status_code == 200
        assert theirs.status_code == 404

    def test_they_cannot_take_money_from_another_routes_shop(self, people, client, invoices, shops):
        """The write side of the same door. A 404 before anything is created."""
        client.force_login(people["Booker"])
        response = client.post(
            url("payments:recovery-receive", pk=shops["other"].pk),
            {"amount": "1000", "mode": "CASH", "posting_date": MAY.isoformat()},
        )
        assert response.status_code == 404

    def test_the_client_autocomplete_only_offers_their_own_routes(
        self, people, client, invoices, shops
    ):
        client.force_login(people["Booker"])
        body = client.get(url("payments:client-search"), {"q": "a"}).content.decode()

        assert shops["own"].name in body
        assert shops["other"].name not in body

    def test_a_sales_document_on_another_route_is_a_404(self, people, client, invoices):
        client.force_login(people["Booker"])

        mine = client.get(url("sales:detail", slug="invoices", pk=invoices["own"].pk))
        theirs = client.get(url("sales:detail", slug="invoices", pk=invoices["other"].pk))

        assert mine.status_code == 200
        assert theirs.status_code == 404

    def test_an_accountant_sees_every_route(self, people, client, invoices, shops):
        """The scope is the *absence* of view_all_routes, not the presence of a seller."""
        client.force_login(people["Accountant"])
        body = client.get(url("payments:recovery")).content.decode()

        assert shops["own"].name in body
        assert shops["other"].name in body

    def test_a_scoped_login_with_no_seller_sees_nothing_rather_than_everything(
        self, django_user_model, client, invoices, shops
    ):
        """The safe failure, asserted because the unsafe one is one line away.

        A Booker whose login was never linked to a seller has no routes. The
        answer has to be an empty sheet — not the whole customer book, which is
        what a filter that skipped itself when the list was empty would give.
        """
        ensure_groups()
        orphan = make_user(django_user_model, "role-unlinked", "Booker")
        client.force_login(orphan)

        body = client.get(url("payments:recovery")).content.decode()
        assert shops["own"].name not in body
        assert shops["other"].name not in body

    def test_scope_survives_the_seller_moving_route(
        self, people, client, invoices, shops, routes, sellers
    ):
        """It is read from RouteSeller each time, never copied onto the user."""
        client.force_login(people["Booker"])
        RouteSeller.objects.create(route=routes["other"], seller=sellers["ours"])

        body = client.get(url("payments:recovery")).content.decode()
        assert shops["other"].name in body


# ===========================================================================
# Cost prices
# ===========================================================================
class TestCostPriceMasking:
    """Masked on the screen is not enough. The export is the usual leak."""

    #: A report whose columns include what the goods cost.
    SLUG = "item-sales"

    def _params(self):
        return {"date_from": APRIL.isoformat(), "date_to": TODAY.isoformat()}

    def test_the_report_declares_cost_columns_as_sensitive(self):
        report = get_report(self.SLUG)
        sensitive = {column.key for column in report.columns if column.sensitive}
        assert {"cost", "margin"} <= sensitive

    def test_an_accountant_sees_the_cost_columns_on_screen(self, people, client, invoices):
        client.force_login(people["Accountant"])
        body = client.get(url("reports:report", slug=self.SLUG), self._params()).content.decode()
        assert "Margin" in body
        assert "Cost" in body

    def test_a_booker_does_not(self, people, client, invoices):
        client.force_login(people["Booker"])
        body = client.get(url("reports:report", slug=self.SLUG), self._params()).content.decode()
        assert "Margin" not in body
        assert "Revenue" in body, "the rest of the report still renders"

    def test_the_csv_export_does_not_leak_it(self, people, client, invoices):
        """**The export-leak case.**

        A column dropped from the table and still written into the file is the
        way this normally goes wrong: the export is built from the column list
        rather than from the template, so guarding the template guards nothing.
        Both are built from ``Report.columns_for`` here, and this is what
        asserts it.
        """
        client.force_login(people["Booker"])
        response = client.get(
            url("reports:report", slug=self.SLUG), {**self._params(), "format": "csv"}
        )
        assert response.status_code == 200

        body = response.content.decode("utf-8-sig")
        header = next(csv.reader(io.StringIO(body)))
        assert "Margin" not in header
        assert "Cost" not in header
        assert "Revenue" in header

        # Every row is the width of the masked header, so the figures went with
        # the heading rather than sliding under the wrong column.
        rows = list(csv.reader(io.StringIO(body)))[1:]
        assert all(len(row) == len(header) for row in rows)

    def test_the_pdf_export_does_not_leak_it_either(self, people, client, invoices):
        """The same leak, one layer further away — and a PDF gets emailed."""
        from tests.test_pdf import text_of

        client.force_login(people["Booker"])
        response = client.get(
            url("reports:report", slug=self.SLUG), {**self._params(), "format": "pdf"}
        )
        assert response.status_code == 200

        text = text_of(response.content)
        assert "Margin" not in text
        assert "Revenue" in text

    def test_an_accountants_csv_still_carries_it(self, people, client, invoices):
        """The masking is a refusal, not a feature that removed the column."""
        client.force_login(people["Accountant"])
        response = client.get(
            url("reports:report", slug=self.SLUG), {**self._params(), "format": "csv"}
        )
        header = next(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
        assert "Margin" in header
        assert "Cost" in header

    def test_the_cost_of_goods_line_is_off_the_sales_screen(self, people, client, invoices):
        client.force_login(people["Booker"])
        body = client.get(
            url("sales:detail", slug="invoices", pk=invoices["own"].pk)
        ).content.decode()
        assert "Cost" not in body

    def test_every_cost_bearing_report_marks_its_columns(self):
        """A cost column that forgot the flag is a cost column that leaks.

        Matched on the column *label*, because that is what a person reads and
        what a report author would have to type — a new "Purchase rate" column
        without ``sensitive=True`` fails here rather than in production.
        """
        leaky = []
        for report in REPORTS.values():
            for column in report.columns:
                looks_like_cost = any(word in column.label.lower() for word in ("cost", "margin"))
                if looks_like_cost and not column.sensitive:
                    leaky.append(f"{report.slug}.{column.key}")
        assert not leaky, f"cost columns not marked sensitive: {leaky}"


# ===========================================================================
# The sidebar, the groups, and the first password
# ===========================================================================
class TestTheOverrides:
    """The two permissions that let somebody past a refusal."""

    def test_only_an_accountant_or_admin_may_pass_a_credit_limit(self, people):
        from apps.sales.services import may_override_credit_limit

        for name, person in people.items():
            expected = name in ("Admin", "Accountant")
            assert may_override_credit_limit(person) is expected, name

    def test_stock_cannot_go_negative_without_the_permission(
        self, people, stock, shops, warehouses, oil, settings
    ):
        """The warehouse holds a hundred cartons. Ask for two hundred."""
        from apps.accounting.exceptions import InsufficientStock

        settings.ALLOW_NEGATIVE_STOCK = False
        document = sales.create_sales_invoice(
            client=shops["own"], warehouse=warehouses.main, posting_date=MAY
        )
        sales.update_line(
            SalesInvoiceLine(document=document),
            item=oil,
            # Twice what the warehouse holds, and cheap enough that the credit
            # limit is not what refuses it — this is a stock test.
            qty_input=200,
            unit_input=Unit.CARTON,
            rate_input_paisa=to_paisa("20"),
        ).save()

        with pytest.raises(InsufficientStock):
            sales.post_sales_invoice(document, user=people["Operator"])

    def test_the_permission_lets_it_through(self, people, stock, shops, warehouses, oil, settings):
        settings.ALLOW_NEGATIVE_STOCK = False
        document = sales.create_sales_invoice(
            client=shops["own"], warehouse=warehouses.main, posting_date=MAY
        )
        sales.update_line(
            SalesInvoiceLine(document=document),
            item=oil,
            # Twice what the warehouse holds, and cheap enough that the credit
            # limit is not what refuses it — this is a stock test.
            qty_input=200,
            unit_input=Unit.CARTON,
            rate_input_paisa=to_paisa("20"),
        ).save()

        sales.post_sales_invoice(document, user=people["Admin"])
        document.refresh_from_db()
        assert document.status == "POSTED"


class TestTheSidebarAgreesWithTheViews:
    def test_a_booker_is_not_offered_the_modules_they_cannot_open(self, people, client, invoices):
        """A menu that offers a refusal is worse than no menu."""
        client.force_login(people["Booker"])
        body = client.get(url("payments:recovery")).content.decode()

        assert url("reports:index") in body
        assert url("purchasing:list", slug="invoices") not in body

    def test_an_operator_is_offered_purchasing(self, people, client, invoices):
        client.force_login(people["Operator"])
        body = client.get(url("sales:list", slug="invoices")).content.decode()
        assert url("purchasing:list", slug="invoices") in body

    def test_the_report_index_lists_only_what_opens(self, people, client, invoices):
        client.force_login(people["Booker"])
        body = client.get(url("reports:index")).content.decode()

        assert "Route Day Sheet" in body
        assert "Balance Sheet" not in body
        assert "Trial Balance" not in body


class TestTheGroups:
    def test_every_named_permission_exists(self):
        """A misspelled permission fails open, so it is caught here."""
        perms.assert_permissions_exist(Permission, perms.every_permission())

    def test_the_five_groups_are_seeded(self):
        ensure_groups()
        assert set(Group.objects.values_list("name", flat=True)) >= set(GROUP_NAMES)

    def test_admin_holds_everything(self):
        ensure_groups()
        admin = Group.objects.get(name="Admin")
        assert admin.permissions.count() == Permission.objects.count()

    def test_seeding_again_changes_nothing(self):
        """Idempotent, because ``migrate`` runs it on every upgrade."""
        ensure_groups()
        before = {
            group.name: set(group.permissions.values_list("pk", flat=True))
            for group in Group.objects.filter(name__in=GROUP_NAMES)
        }
        seed_groups(Group, Permission)
        after = {
            group.name: set(group.permissions.values_list("pk", flat=True))
            for group in Group.objects.filter(name__in=GROUP_NAMES)
        }
        assert before == after

    def test_seeding_never_takes_a_permission_away(self):
        """A tuned installation keeps its tuning across an upgrade."""
        ensure_groups()
        viewer = Group.objects.get(name="Viewer")
        extra = Permission.objects.get(content_type__app_label="sales", codename="add_salesinvoice")
        viewer.permissions.add(extra)

        seed_groups(Group, Permission)

        assert extra in viewer.permissions.all()

    @pytest.mark.parametrize("spec", GROUP_DEFINITIONS, ids=lambda spec: spec.name)
    def test_no_group_names_a_permission_that_does_not_exist(self, spec):
        if spec.everything:
            return
        perms.assert_permissions_exist(Permission, spec.permissions)

    def test_only_the_admin_can_touch_the_backup(self, people):
        for name, person in people.items():
            expected = name == "Admin"
            assert has_access(person, perms.RUN_BACKUP) is expected, name
            assert has_access(person, perms.RESTORE_BACKUP) is expected, name

    def test_only_the_booker_is_route_scoped(self, people):
        from apps.accounts.scoping import is_route_scoped

        for name, person in people.items():
            scoped = name in ("Booker", "Nobody")
            assert is_route_scoped(person) is scoped, name

    def test_a_deactivated_login_holds_nothing(self, people):
        accountant = people["Accountant"]
        assert has_access(accountant, "payments.view_payment")

        accountant.is_active = False
        accountant.save(update_fields=["is_active"])

        assert not has_access(accountant, "payments.view_payment")


class TestTheFirstPassword:
    """A login created by an administrator must not stay on their password."""

    def test_a_new_login_is_sent_to_the_password_screen(self, people, client, django_user_model):
        user = people["Operator"]
        UserProfile.for_user(user).require_password_change(user)
        client.force_login(user)

        response = client.get(url("sales:list", slug="invoices"))

        assert response.status_code == 302
        assert response["Location"] == reverse("admin:password_change")

    def test_the_password_screen_itself_stays_reachable(self, people, client):
        user = people["Operator"]
        UserProfile.for_user(user).require_password_change(user)
        client.force_login(user)

        assert client.get(reverse("admin:password_change")).status_code == 200

    def test_changing_it_clears_the_flag_however_it_was_changed(self, people, client):
        """No signal, no view override: the hash is compared.

        Which means a password changed from a shell or a management command
        satisfies this too, rather than only the one screen somebody remembered
        to hook.
        """
        user = people["Operator"]
        UserProfile.for_user(user).require_password_change(user)

        user.set_password("a-new-one-entirely")
        user.save(update_fields=["password"])

        client.force_login(user)
        assert client.get(url("sales:list", slug="invoices")).status_code == 200
        assert not UserProfile.for_user(user).must_change_password

    def test_an_ordinary_login_is_not_pestered(self, people, client, invoices):
        """The flag is off by default, so an upgrade does not lock everybody out."""
        client.force_login(people["Operator"])
        assert client.get(url("sales:list", slug="invoices")).status_code == 200


class TestTheUserAdmin:
    def test_creating_a_user_demands_a_password_change(
        self, admin_client_logged_in, django_user_model
    ):
        response = admin_client_logged_in.post(
            reverse("admin:auth_user_add"),
            {
                "username": "newhire",
                "password1": "correct-horse-battery",
                "password2": "correct-horse-battery",
            },
        )
        assert response.status_code in (200, 302)

        user = django_user_model.objects.get(username="newhire")
        assert UserProfile.for_user(user).must_change_password

    def test_the_user_list_opens_for_an_administrator(self, admin_client_logged_in):
        assert admin_client_logged_in.get(reverse("admin:auth_user_changelist")).status_code == 200

    def test_deactivating_is_how_somebody_is_removed(self, people, admin_client_logged_in):
        """Never deleted: a user is named on every document they ever touched.

        Driven through the real changelist action rather than by calling the
        method, so the permission check and the "leave your own alone" guard are
        both on the path being tested.
        """
        user = people["Viewer"]
        admin_client_logged_in.post(
            reverse("admin:auth_user_changelist"),
            {"action": "deactivate", "_selected_action": [str(user.pk)]},
            follow=True,
        )
        user.refresh_from_db()
        assert not user.is_active

    def test_deactivating_leaves_your_own_login_alone(
        self, admin_client_logged_in, django_user_model
    ):
        """Locking yourself out of the only screen that could let you back in."""
        me = django_user_model.objects.get(username="admin")
        admin_client_logged_in.post(
            reverse("admin:auth_user_changelist"),
            {"action": "deactivate", "_selected_action": [str(me.pk)]},
            follow=True,
        )
        me.refresh_from_db()
        assert me.is_active
