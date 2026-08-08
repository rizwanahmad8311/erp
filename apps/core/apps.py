from django.apps import AppConfig


class CoreConfig(AppConfig):
    """Shared utilities, abstract base models, money and quantity fields."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.core"
    label = "core"
    verbose_name = "Core"
