"""Create the five groups and give them their permissions.

Runs after every app that declares a permission, because a group cannot be given
one that does not exist yet.

The awkward part is that **permissions are not created by migrations**. Django
creates them in a ``post_migrate`` signal handler, which fires after *all*
migrations have run — so a data migration that looks for ``sales.post_salesinvoice``
mid-migrate finds nothing, and the seed silently produces five empty groups on a
fresh database and a correct one on an existing database. That is the failure
this migration is arranged to avoid: :func:`_ensure_permissions` runs the same
handler early, for every app, before the groups are touched.
"""

from django.apps import apps as global_apps
from django.contrib.auth.management import create_permissions
from django.db import migrations

from apps.accounts.groups import seed_groups


def _ensure_permissions(using: str) -> None:
    """Run Django's own permission creation now rather than after the migrate.

    ``create_permissions`` returns immediately for an app whose ``models_module``
    is unset, which is how it is during a migrate — hence the temporary flag.
    This is the documented workaround and it is idempotent: the handler will run
    again at the end of the migrate and find nothing left to do.
    """
    for app_config in global_apps.get_app_configs():
        previous = app_config.models_module
        app_config.models_module = True
        try:
            create_permissions(app_config, using=using, verbosity=0)
        finally:
            app_config.models_module = previous


def forwards(apps, schema_editor):
    using = schema_editor.connection.alias
    _ensure_permissions(using)

    # Historical models, not the live ones: a migration that imports the live
    # model breaks the day the model gains a field. Same reasoning as the chart
    # of accounts seed.
    group_model = apps.get_model("auth", "Group")
    permission_model = apps.get_model("auth", "Permission")
    seed_groups(group_model, permission_model)


def backwards(apps, schema_editor):
    """Deliberately does nothing.

    Deleting the five groups would take every user's access with them —
    ``Group`` is the join table, so unassigning is what deletion means here —
    and re-applying this migration would create the groups again but could not
    put anybody back in one. A group nobody is in is harmless; a staff list that
    quietly lost its permissions is not.
    """


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0001_initial"),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
        # Every app that declares a permission the groups ask for.
        ("accounting", "0006_alter_stockentry_options"),
        ("backup", "0001_initial"),
        ("masters", "0004_alter_item_options"),
        ("payments", "0003_alter_chequeevent_options_alter_payment_options"),
        ("purchasing", "0003_alter_purchaseinvoice_options_and_more"),
        ("reports", "0002_reportaccess"),
        ("sales", "0003_alter_salesinvoice_options_alter_salesreturn_options"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
