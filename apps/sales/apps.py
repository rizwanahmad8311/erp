from django.apps import AppConfig


class SalesConfig(AppConfig):
    """Sales orders, invoices, deliveries and returns."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.sales"
    label = "sales"
    verbose_name = "Sales"
