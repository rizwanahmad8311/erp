from django.apps import AppConfig


class PaymentsConfig(AppConfig):
    """Receipts, payments, recovery against invoices."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.payments"
    label = "payments"
    verbose_name = "Payments"
