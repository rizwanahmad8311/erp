from django.apps import AppConfig


class ReportsConfig(AppConfig):
    """Read-only reporting over the ledger tables."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.reports"
    label = "reports"
    verbose_name = "Reports"
