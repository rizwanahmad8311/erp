from django.apps import AppConfig


class MastersConfig(AppConfig):
    """Items, UOM, parties, routes and sellers."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.masters"
    label = "masters"
    verbose_name = "Masters"
