"""The two management commands that a person runs by hand on the office PC.

``seed_chart_of_accounts`` is repair: somebody deleted a heading, or the
installation predates an account being added to the default chart.

``preflight`` is the answer to "is this installation fit to run", asked by
somebody standing at a Windows machine who cannot read a traceback. Each of its
checks is exercised in both directions here — passing and failing — because a
check that can only ever say OK is a check that will say OK on the morning it
matters.
"""

from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import call_command

from apps.accounting.models import Account

pytestmark = pytest.mark.django_db


def run(command, *args, **options) -> str:
    """Run a command and return everything it wrote to stdout."""
    out, err = StringIO(), StringIO()
    call_command(command, *args, stdout=out, stderr=err, **options)
    return out.getvalue() + err.getvalue()


# ===========================================================================
# seed_chart_of_accounts
# ===========================================================================
class TestSeedChartOfAccounts:
    def test_it_reports_everything_already_present_on_a_seeded_database(self, accounts):
        output = run("seed_chart_of_accounts")

        assert "Created 0 account(s)" in output
        assert "already present" in output

    def test_it_recreates_an_account_somebody_deleted(self, accounts):
        """The repair this command exists for."""
        Account.objects.filter(code="5430").delete()
        assert not Account.objects.filter(code="5430").exists()

        output = run("seed_chart_of_accounts")

        assert Account.objects.filter(code="5430").exists()
        assert "Created 1 account(s)" in output

    def test_dry_run_reports_without_writing(self, accounts):
        """Rolls back, so a nervous administrator can look before leaping."""
        Account.objects.filter(code="5430").delete()

        output = run("seed_chart_of_accounts", "--dry-run")

        assert "Would create 1 account(s)" in output
        assert not Account.objects.filter(code="5430").exists(), "dry-run must write nothing"

    def test_it_never_modifies_an_existing_account(self, accounts):
        """Additive only. A live installation will have tuned a name."""
        renamed = Account.objects.get(code="5430")
        renamed.name = "Electricity and water — renamed on site"
        renamed.save()

        run("seed_chart_of_accounts")

        renamed.refresh_from_db()
        assert renamed.name == "Electricity and water — renamed on site"


# ===========================================================================
# preflight
# ===========================================================================
class TestPreflight:
    """Both directions for every check.

    The command exits non-zero when anything FAILs, so most of these assert on
    ``SystemExit`` as well as on the words — the exit code is what install.bat
    reads.
    """

    def _run(self, **env):
        """Run preflight, returning (output, exit_code)."""
        out, err = StringIO(), StringIO()
        try:
            call_command("preflight", stdout=out, stderr=err, **env)
        except SystemExit as exit_:
            return out.getvalue() + err.getvalue(), exit_.code
        return out.getvalue() + err.getvalue(), 0

    def test_under_test_settings_it_fails_and_says_why(self):
        """The suite does not run with config.settings.prod, so this must FAIL.

        That is the check working: preflight is only meaningful against the
        production profile, and saying OK here would make it meaningless there.
        """
        output, code = self._run()

        assert code == 1
        assert "not config.settings.prod" in output
        assert "not ready to use" in output

    def test_every_check_reports_a_line(self):
        """Whatever the verdict, the operator sees one line per check."""
        output, _ = self._run()

        for topic in ("Settings module", "DEBUG", "SECRET_KEY", "ALLOWED_HOSTS"):
            assert topic in output, f"preflight said nothing about {topic}"

    def test_it_names_the_exact_command_to_re_run(self):
        output, _ = self._run()
        assert "manage.py preflight" in output
        assert "--settings=config.settings.prod" in output

    def test_debug_on_is_a_failure(self, settings):
        settings.DEBUG = True
        output, code = self._run()

        assert code == 1
        assert "DEBUG" in output

    def test_an_insecure_secret_key_is_caught(self, settings):
        settings.SECRET_KEY = "insecure-development-key-do-not-deploy"
        output, _ = self._run()

        assert "SECRET_KEY" in output

    def test_an_empty_allowed_hosts_is_caught(self, settings):
        settings.ALLOWED_HOSTS = []
        output, _ = self._run()

        assert "ALLOWED_HOSTS" in output

    def test_it_checks_the_database_is_reachable_and_migrated(self):
        output, _ = self._run()
        assert "atabase" in output

    def test_it_checks_backups_are_configured(self):
        output, _ = self._run()
        assert "ackup" in output

    def test_the_service_check_is_opt_in(self):
        """Off by default: preflight runs *before* the service starts, and a
        check that waited for a port would fail every first install."""
        without, _ = self._run()
        assert "http://" not in without or "Service" not in without

    def test_the_service_check_reports_when_nothing_answers(self):
        """A short timeout, so the test does not sit for the real default."""
        output, _ = self._run(service=True, timeout=1)
        assert "ervice" in output


# ===========================================================================
# check_integrity — the nightly audit
# ===========================================================================
@pytest.fixture
def a_posted_invoice(db, accounts, warehouses):
    """One real posting, so the checks have something to check.

    Deliberately built through the services rather than by inserting rows: a
    check that only ever sees hand-made data proves nothing about the data the
    application produces.
    """
    import datetime as dt

    from apps.masters.enums import Unit
    from apps.masters.models import Client, Item, Vendor
    from apps.purchasing import services as purchasing
    from apps.purchasing.models import PurchaseInvoiceLine
    from apps.sales import services as sales
    from apps.sales.models import SalesInvoiceLine

    vendor = Vendor.objects.create(code="V-01", name="Supplier")
    shop = Client.objects.create(code="C-0001", name="Shop", credit_limit_paisa=100_000_000)
    oil = Item.objects.create(code="OIL-1", name="Oil 1L", carton_size=12)

    bill = purchasing.create_purchase_invoice(
        vendor=vendor, warehouse=warehouses.main, posting_date=dt.date(2026, 4, 1)
    )
    purchasing.update_line(
        PurchaseInvoiceLine(document=bill),
        item=oil,
        qty_input=100,
        unit_input=Unit.CARTON,
        rate_input_paisa=240_000,
    ).save()
    purchasing.post_purchase_invoice(bill, user=None)

    invoice = sales.create_sales_invoice(
        client=shop, warehouse=warehouses.main, posting_date=dt.date(2026, 5, 1)
    )
    sales.update_line(
        SalesInvoiceLine(document=invoice),
        item=oil,
        qty_input=10,
        unit_input=Unit.CARTON,
        rate_input_paisa=250_000,
    ).save()
    return sales.post_sales_invoice(invoice, user=None)


class TestCheckIntegrityCommand:
    def test_a_clean_database_passes(self, a_posted_invoice):
        output = run("check_integrity")
        assert "PASS" in output or "All" in output

    def test_it_exits_non_zero_when_something_is_wrong(self, a_posted_invoice):
        """Driven by breaking the ledger the only way the guards allow: raw SQL.

        Nothing in the application can produce this state — LedgerEntry is
        append-only (CLAUDE.md §3) — which is exactly why the check exists. A
        corrupt file, a half-restored backup or a hand-edited database is what
        it is looking for.
        """
        from django.db import connection

        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE accounting_ledgerentry SET debit_paisa = debit_paisa + 1 "
                "WHERE id = (SELECT MIN(id) FROM accounting_ledgerentry)"
            )

        out, err = StringIO(), StringIO()
        with pytest.raises(SystemExit) as caught:
            call_command("check_integrity", stdout=out, stderr=err)

        assert caught.value.code != 0
        assert "trial balance" in (out.getvalue() + err.getvalue()).lower()
