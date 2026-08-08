from django.apps import AppConfig


class TestAppConfig(AppConfig):
    """Test-only app; installed by config/settings/test.py and nothing else."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "tests.testapp"
    label = "testapp"
