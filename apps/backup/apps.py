from django.apps import AppConfig


class BackupConfig(AppConfig):
    """Scheduled SQLite backup and restore for the Windows box."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.backup"
    label = "backup"
    verbose_name = "Backup"
