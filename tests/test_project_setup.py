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
        """
        from django.apps import apps as django_apps

        accounting_models = {
            m.__name__ for m in django_apps.get_app_config("accounting").get_models()
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

        masters_models = {m.__name__ for m in django_apps.get_app_config("masters").get_models()}
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

    def test_no_other_domain_models_exist_yet(self):
        """core may hold infrastructure (DocumentSequence); the remaining domain
        apps hold nothing at all until their models are designed."""
        from django.apps import apps as django_apps

        domain_labels = {
            "purchasing",
            "sales",
            "payments",
            "reports",
            "backup",
        }
        models = [m for m in django_apps.get_models() if m._meta.app_label in domain_labels]
        assert models == [], f"Unexpected models: {models}"

    def test_core_holds_only_infrastructure(self):
        from django.apps import apps as django_apps

        core_models = {m.__name__ for m in django_apps.get_app_config("core").get_models()}
        assert core_models == {"DocumentSequence"}, (
            f"core should hold only the sequence counter, found: {sorted(core_models)}"
        )
