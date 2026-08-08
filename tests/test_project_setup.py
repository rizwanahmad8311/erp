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


class TestMoneyHelpers:
    def test_paisa_round_trip(self):
        from apps.core.money import to_paisa, to_rupees

        assert to_paisa("123.45") == 12345
        assert to_paisa(0.1) == 10
        assert str(to_rupees(12345)) == "123.45"

    def test_half_up_rounding_at_the_boundary(self):
        from apps.core.money import to_paisa

        assert to_paisa("0.005") == 1  # banker's rounding would give 0

    def test_format_is_display_only(self):
        from apps.core.money import format_money

        assert format_money(123456789) == "Rs 1,234,567.89"
        assert format_money(-12345) == "Rs -123.45"

    @pytest.mark.parametrize(
        ("total", "parts"),
        [(100, 3), (1, 4), (9999, 7), (-100, 3), (0, 5)],
    )
    def test_split_evenly_never_loses_a_paisa(self, total, parts):
        from apps.core.money import split_evenly

        assert sum(split_evenly(total, parts)) == total


class TestFieldTypes:
    def test_money_field_is_big_integer(self):
        from django.db.models import BigIntegerField

        from apps.core.fields import MoneyField

        assert issubclass(MoneyField, BigIntegerField)

    def test_quantity_field_is_big_integer(self):
        from django.db.models import BigIntegerField

        from apps.core.fields import QuantityField

        assert issubclass(QuantityField, BigIntegerField)

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

    def test_no_business_models_exist_yet(self):
        from django.apps import apps as django_apps

        domain_labels = {
            "accounting",
            "masters",
            "purchasing",
            "sales",
            "payments",
            "reports",
            "backup",
            "core",
        }
        models = [m for m in django_apps.get_models() if m._meta.app_label in domain_labels]
        assert models == [], f"Unexpected models: {models}"
