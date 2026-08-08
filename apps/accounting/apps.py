from django.apps import AppConfig


class AccountingConfig(AppConfig):
    """Chart of accounts and the append-only general ledger."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.accounting"
    label = "accounting"
    verbose_name = "Accounting"
