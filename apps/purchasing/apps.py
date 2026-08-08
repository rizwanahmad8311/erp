from django.apps import AppConfig


class PurchasingConfig(AppConfig):
    """Purchase orders, goods receipts and supplier bills."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.purchasing"
    label = "purchasing"
    verbose_name = "Purchasing"
