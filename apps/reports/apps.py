from django.apps import AppConfig


class ReportsConfig(AppConfig):
    """Read-only reporting over the ledger tables."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.reports"
    label = "reports"
    verbose_name = "Reports"

    def ready(self):
        """Register the report catalogue.

        The reports register themselves at import time, and this is the one
        place that import happens — so the index, the URLs and the sidebar are
        all built from a list nobody maintains by hand.

        ``ready()`` rather than module scope: the catalogue imports models from
        five apps, and doing that while ``apps.reports`` is still being loaded
        would be an import-order dependency that works until somebody reorders
        ``INSTALLED_APPS``.
        """
        from . import catalog  # noqa: F401  (imported for the side effect)
