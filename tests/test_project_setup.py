"""
Guardrail tests for the locked decisions in CLAUDE.md.

These are deliberately about configuration rather than behaviour: they fail the
moment someone loosens a rule the whole ledger depends on.
"""

import shutil
import subprocess
from pathlib import Path

import pytest
from django.conf import settings


class TestDatabaseConfiguration:
    def test_sqlite_backend(self):
        assert settings.DATABASES["default"]["ENGINE"] == "django.db.backends.sqlite3"

    def test_immediate_transaction_mode(self):
        """IMMEDIATE takes the write lock at BEGIN, closing the SQLITE_BUSY
        window between two concurrent postings."""
        options = settings.DATABASES["default"]["OPTIONS"]
        assert options["transaction_mode"] == "IMMEDIATE"

    def test_wal_and_synchronous_normal(self):
        init = settings.DATABASES["default"]["OPTIONS"]["init_command"]
        assert "journal_mode=WAL" in init
        assert "synchronous=NORMAL" in init

    def test_busy_timeout_present(self):
        assert settings.DATABASES["default"]["OPTIONS"]["timeout"] == 20


class TestStaticFiles:
    def test_dist_is_the_only_static_source(self):
        """src/ is authored input; only dist/ is served."""
        dirs = [Path(p) for p in settings.STATICFILES_DIRS]
        assert dirs == [Path(settings.BASE_DIR) / "static" / "dist"]

    def test_compiled_css_is_committed(self):
        css = Path(settings.BASE_DIR) / "static" / "dist" / "app.css"
        assert css.exists(), "static/dist/app.css must be committed; prod cannot build it"

    def test_whitenoise_is_installed(self):
        assert "whitenoise.middleware.WhiteNoiseMiddleware" in settings.MIDDLEWARE

    def test_dist_is_not_git_ignored(self):
        """A bare `dist/` rule in .gitignore silently swallows static/dist and
        ships an unstyled site to a machine that cannot rebuild it."""
        if shutil.which("git") is None:
            pytest.skip("git not available")
        base = Path(settings.BASE_DIR)
        if not (base / ".git").exists():
            pytest.skip("not a git working tree")
        for rel in ("static/dist/app.css", "static/dist/js/app.js"):
            result = subprocess.run(["git", "check-ignore", "-q", rel], cwd=base, check=False)
            assert result.returncode != 0, f"{rel} is git-ignored but must be committed"


class TestNoExternalAssets:
    """No CDN references anywhere. The production PC has no internet."""

    SUSPECT = (
        "https://cdn",
        "http://cdn",
        "cdnjs.cloudflare",
        "unpkg.com",
        "jsdelivr",
        "fonts.googleapis",
        "fonts.gstatic",
        "ajax.googleapis",
        "bootstrapcdn",
    )

    def _sources(self):
        base = Path(settings.BASE_DIR)
        for pattern in (
            "templates/**/*.html",
            "apps/**/*.html",
            "static/dist/**/*.css",
            "static/dist/**/*.js",
            "static/src/**/*.css",
            "static/src/**/*.js",
        ):
            yield from base.glob(pattern)

    def test_no_cdn_urls_in_templates_or_assets(self):
        offenders = []
        for path in self._sources():
            text = path.read_text(encoding="utf-8", errors="ignore").lower()
            for needle in self.SUSPECT:
                if needle in text:
                    offenders.append(f"{path}: {needle}")
        assert not offenders, "External asset references found:\n" + "\n".join(offenders)


class TestFieldTypes:
    """Behaviour of the fields themselves lives in tests/test_money.py; these
    only pin the storage types the whole ledger depends on."""

    def test_money_field_is_big_integer(self):
        from django.db.models import BigIntegerField

        from apps.core.fields import MoneyField

        assert issubclass(MoneyField, BigIntegerField)

    def test_quantity_field_is_a_plain_integer(self):
        """32-bit: piece counts never approach two billion, and the narrower
        column makes a paisa value assigned to a quantity fail loudly."""
        from django.db.models import BigIntegerField, IntegerField

        from apps.core.fields import QuantityField

        assert issubclass(QuantityField, IntegerField)
        assert not issubclass(QuantityField, BigIntegerField)

    def test_no_decimal_or_float_fields_declared_anywhere(self):
        """Money must never be a DecimalField or FloatField in the database."""
        offenders = []
        for path in Path(settings.BASE_DIR).glob("apps/**/*.py"):
            if "migrations" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            for banned in ("models.DecimalField", "models.FloatField"):
                if banned in text:
                    offenders.append(f"{path}: {banned}")
        assert not offenders, "Money/qty must be integer fields:\n" + "\n".join(offenders)


class TestDocumentLifecycle:
    def test_transitions_are_locked(self):
        from apps.core.enums import ALLOWED_STATUS_TRANSITIONS, DocumentStatus

        assert ALLOWED_STATUS_TRANSITIONS[DocumentStatus.DRAFT] == {DocumentStatus.POSTED}
        assert ALLOWED_STATUS_TRANSITIONS[DocumentStatus.POSTED] == {DocumentStatus.CANCELLED}
        assert ALLOWED_STATUS_TRANSITIONS[DocumentStatus.CANCELLED] == set()

    def test_base_models_are_abstract(self):
        """No business models yet — the bases must not create tables."""
        from apps.core.models import AppendOnlyModel, DocumentModel, TimeStampedModel

        for model in (TimeStampedModel, AppendOnlyModel, DocumentModel):
            assert model._meta.abstract


class TestAppRegistry:
    def test_all_domain_apps_installed(self):
        expected = {
            "apps.core",
            "apps.accounting",
            "apps.masters",
            "apps.purchasing",
            "apps.sales",
            "apps.payments",
            "apps.reports",
            "apps.backup",
        }
        assert expected <= set(settings.INSTALLED_APPS)

    def test_unfold_precedes_admin(self):
        apps = settings.INSTALLED_APPS
        assert apps.index("unfold") < apps.index("django.contrib.admin")

    def test_accounting_holds_the_two_ledgers(self):
        """Everything else in the system is derived from these four tables.

        The stock ledger lives beside the general ledger rather than in an app
        of its own because it is the same thing: an append-only record that
        every report is aggregated from. The chart and the warehouse list are
        the masters those records hang off.

        ``HistoricalAccount`` is generated by django-simple-history and is not a
        design decision of this app's; :class:`TestMasterHistory` is what pins
        which models have one.
        """
        from django.apps import apps as django_apps

        accounting_models = {
            m.__name__
            for m in django_apps.get_app_config("accounting").get_models()
            if not m.__name__.startswith("Historical")
        }
        assert accounting_models == {"Account", "LedgerEntry", "Warehouse", "StockEntry"}, (
            f"accounting should hold both ledgers, found: {sorted(accounting_models)}"
        )

    def test_both_ledgers_are_append_only(self):
        """The guard is inherited, not re-implemented per model, so it cannot
        drift between LedgerEntry and StockEntry."""
        from apps.accounting.models import LedgerEntry, StockEntry
        from apps.core.models import AppendOnlyModel

        assert issubclass(LedgerEntry, AppendOnlyModel)
        assert issubclass(StockEntry, AppendOnlyModel)

    def test_masters_holds_the_master_data_and_no_balances(self):
        """Items, categories, parties, routes and sellers — and nothing else.

        Documents live in the transaction apps and the two ledgers live in
        accounting. A model appearing here that records a *movement* rather than
        a *thing* is the first step towards a balance being cached on a master,
        which CLAUDE.md §6 forbids.
        """
        from django.apps import apps as django_apps

        masters_models = {
            m.__name__
            for m in django_apps.get_app_config("masters").get_models()
            if not m.__name__.startswith("Historical")
        }
        assert masters_models == {
            "Item",
            "ItemCategory",
            "Client",
            "Vendor",
            "Route",
            "Seller",
            "RouteSeller",
        }, f"unexpected masters models: {sorted(masters_models)}"

    def test_no_master_caches_a_balance_or_a_stock_level(self):
        """The rule that stops masters quietly becoming a second ledger.

        Every balance, every outstanding and every stock level is aggregated
        from LedgerEntry and StockEntry (CLAUDE.md §6). ``opening_balance_paisa``
        is the one permitted exception and is not a balance: it is what the
        operator typed at go-live, read by the opening voucher and by nothing
        else.
        """
        from django.apps import apps as django_apps

        allowed = {"opening_balance_paisa", "credit_limit_paisa"}
        banned = ("balance", "outstanding", "stock", "on_hand", "total")
        offenders = [
            f"{model.__name__}.{field.name}"
            for model in django_apps.get_app_config("masters").get_models()
            for field in model._meta.get_fields()
            if getattr(field, "attname", None)
            and field.name not in allowed
            and any(needle in field.name for needle in banned)
        ]
        assert not offenders, (
            f"Masters must not cache ledger-derived figures: {offenders}. "
            f"Aggregate from the ledger instead."
        )

    def test_purchasing_holds_two_documents_and_their_lines(self):
        """A document and its lines, per document type. Nothing else.

        No ``Payment``, no ``GoodsReceipt``, no allocation table — those belong
        to the apps that own them, and a model invented in advance of the app
        that owns it is a model that gets invented wrong.
        """
        from django.apps import apps as django_apps

        purchasing_models = {
            m.__name__ for m in django_apps.get_app_config("purchasing").get_models()
        }
        assert purchasing_models == {
            "PurchaseInvoice",
            "PurchaseInvoiceLine",
            "PurchaseReturn",
            "PurchaseReturnLine",
        }, f"unexpected purchasing models: {sorted(purchasing_models)}"

    def test_every_purchase_document_is_a_DocumentModel(self):
        """Which is what makes DRAFT -> POSTED -> CANCELLED enforced rather than
        remembered, and what makes a posted document immutable."""
        from apps.core.models import DocumentModel
        from apps.purchasing.models import PurchaseInvoice, PurchaseReturn

        for model in (PurchaseInvoice, PurchaseReturn):
            assert issubclass(model, DocumentModel)

    def test_sales_holds_two_documents_and_their_lines(self):
        """The same shape as purchasing, mirrored. Nothing else.

        No ``Payment``, no ``Delivery``, no allocation table — those belong to
        the apps that own them.
        """
        from django.apps import apps as django_apps

        sales_models = {m.__name__ for m in django_apps.get_app_config("sales").get_models()}
        assert sales_models == {
            "SalesInvoice",
            "SalesInvoiceLine",
            "SalesReturn",
            "SalesReturnLine",
        }, f"unexpected sales models: {sorted(sales_models)}"

    def test_every_transaction_document_is_a_DocumentModel(self):
        """Which is what makes DRAFT -> POSTED -> CANCELLED enforced rather than
        remembered, and what makes a posted document immutable."""
        from apps.core.models import DocumentModel
        from apps.purchasing.models import PurchaseInvoice, PurchaseReturn
        from apps.sales.models import SalesInvoice, SalesReturn

        for model in (PurchaseInvoice, PurchaseReturn, SalesInvoice, SalesReturn):
            assert issubclass(model, DocumentModel)

    def test_no_document_caches_what_it_has_been_paid(self):
        """CLAUDE.md §6. ``paid_paisa`` is a property over the payments, and a
        column would be a number that can disagree with them."""
        from apps.purchasing.models import PurchaseInvoice, PurchaseReturn
        from apps.sales.models import SalesInvoice, SalesReturn

        for model in (PurchaseInvoice, PurchaseReturn, SalesInvoice, SalesReturn):
            columns = {field.name for field in model._meta.get_fields()}
            assert "paid_paisa" not in columns
            assert "outstanding_paisa" not in columns

    def test_cost_of_goods_sold_is_stored_on_the_line_not_derived(self):
        """The one figure on a document that deliberately *is* a column.

        Everything else derived from the ledger is a property, because the
        ledger cannot disagree with itself. Cost is different: it is captured
        from the stock valuation at post time and frozen, because the moving
        average moves and an immutable document's margin must not.
        """
        from apps.sales.models import SalesInvoiceLine, SalesReturnLine

        for model in (SalesInvoiceLine, SalesReturnLine):
            field = model._meta.get_field("cogs_paisa")
            assert field.concrete, "cogs_paisa must be a stored column, not a property"

    def test_the_line_arithmetic_has_exactly_one_home(self):
        """Purchasing and sales must not each own a copy of the rounding rule.

        Two implementations of "what does 10 cartons at Rs 2,400 come to" are
        two implementations that will disagree, and the one that drifts is the
        one nobody is looking at.
        """
        from apps.masters import pricing
        from apps.purchasing import services as purchasing
        from apps.sales import services as sales

        assert purchasing.compute_line is pricing.compute_line
        assert sales.compute_line is pricing.compute_line
        assert purchasing.update_line is sales.update_line

    def test_payments_holds_the_money_the_link_and_the_cheque(self):
        """Three models, and the split between them is the design.

        A payment is a document. An allocation is a *link* from that money to
        the bills it settles, which is why it is a separate table and why it
        stays editable after the payment posts. A cheque event is a document of
        its own because clearing happens weeks later and deserves its own date.

        A fourth model here would almost certainly be a cached balance.
        """
        from django.apps import apps as django_apps

        payments_models = {m.__name__ for m in django_apps.get_app_config("payments").get_models()}
        assert payments_models == {"Payment", "PaymentAllocation", "ChequeEvent"}, (
            f"unexpected payments models: {sorted(payments_models)}"
        )

    def test_every_payment_document_is_a_DocumentModel(self):
        """DRAFT -> POSTED -> CANCELLED, enforced rather than remembered.

        The allocation is deliberately *not* one: it writes no ledger row, so
        freezing it when the payment posts would make the recovery workspace —
        where money that arrived last week is applied to bills today —
        impossible.
        """
        from apps.core.models import DocumentModel, TimeStampedModel
        from apps.payments.models import ChequeEvent, Payment, PaymentAllocation

        for model in (Payment, ChequeEvent):
            assert issubclass(model, DocumentModel)
        assert issubclass(PaymentAllocation, TimeStampedModel)
        assert not issubclass(PaymentAllocation, DocumentModel)

    def test_no_payment_caches_what_the_bank_did_with_the_cheque(self):
        """CLAUDE.md §5 and §6, meeting on the same field.

        A ``cheque_status`` column would have to be written onto a POSTED
        document, which the lifecycle forbids, *and* would be a second answer
        that can disagree with the cheque events. It is derived from them.
        """
        from apps.payments.models import Payment

        columns = {field.name for field in Payment._meta.get_fields()}
        for banned in ("cheque_status", "allocated_paisa", "outstanding_paisa", "is_bounced"):
            assert banned not in columns, f"Payment.{banned} must be derived, not stored"

    def test_reports_holds_no_figures_of_its_own(self):
        """apps.reports aggregates the ledger; it stores nothing derived from it.

        ``CompanyProfile`` is the one model it owns and it is not a figure: it
        is the name, address and tax number printed at the top of a page, which
        live nowhere else. A second model here would almost certainly be a
        cached total, which CLAUDE.md §6 forbids.
        """
        from django.apps import apps as django_apps

        reports_models = {m.__name__ for m in django_apps.get_app_config("reports").get_models()}
        assert reports_models == {"CompanyProfile", "ReportAccess"}, (
            f"unexpected reports models: {sorted(reports_models)}"
        )

        # ReportAccess is the exception that proves the rule: it is unmanaged,
        # has no table and holds nothing at all. Django hangs a permission on a
        # model or nowhere, and "may this person open the financial statements"
        # is not a figure.
        from apps.reports.models import ReportAccess

        assert not ReportAccess._meta.managed
        assert [f.name for f in ReportAccess._meta.get_fields()] == ["id"]

    def test_the_company_profile_caches_no_ledger_figure(self):
        from apps.reports.models import CompanyProfile

        banned = ("balance", "outstanding", "stock", "total", "paisa")
        offenders = [
            field.name
            for field in CompanyProfile._meta.get_fields()
            if getattr(field, "attname", None) and any(n in field.name for n in banned)
        ]
        assert not offenders, f"CompanyProfile must hold no figures: {offenders}"

    def test_backup_holds_permissions_and_a_run_log_and_nothing_else(self):
        """Two models, and neither one is business data.

        ``BackupPolicy`` exists only because Django attaches a permission to a
        model. It is unmanaged — ``migrate`` creates no table for it.

        ``BackupLog`` is a record of what the backup command did: which file,
        which destination, whether it worked. **The backup itself is still a
        file on disk, not a row** — a backup you can only find through the
        application is a backup you cannot use on the morning the application
        will not start.
        """
        from django.apps import apps as django_apps

        from apps.backup.models import BackupLog, BackupPolicy

        models = {m.__name__ for m in django_apps.get_models() if m._meta.app_label == "backup"}
        assert models == {"BackupPolicy", "BackupLog"}

        assert not BackupPolicy._meta.managed, "the permission holder stores nothing"
        assert [f.name for f in BackupPolicy._meta.get_fields()] == ["id"]

        # The log records files, never figures. A paisa value in here would be a
        # second place money lives, and it would not be the ledger (CLAUDE.md §6).
        banned = ("paisa", "balance", "amount", "total")
        offenders = [
            f.name
            for f in BackupLog._meta.get_fields()
            if getattr(f, "attname", None) and any(n in f.name for n in banned)
        ]
        assert not offenders, f"the backup log must hold no monetary figure: {offenders}"

    def test_core_holds_only_infrastructure(self):
        from django.apps import apps as django_apps

        core_models = {m.__name__ for m in django_apps.get_app_config("core").get_models()}
        assert core_models == {"DocumentSequence"}, (
            f"core should hold only the sequence counter, found: {sorted(core_models)}"
        )
