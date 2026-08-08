"""Seed the default chart of accounts.

This runs as a migration rather than as a setup step because the deployment
story on the Windows box is exactly five commands and none of them is "now seed
the accounts" (CLAUDE.md §8). A fresh `migrate` has to produce a database the
business can post into.

The seed reads DEFAULT_CHART from the live module rather than freezing a copy
here. That is the opposite of the usual advice for data migrations, and it is
deliberate: this is reference data, not a transformation of user data. Replaying
it on a fresh database should produce today's chart, not the chart as it stood
the week this file was written. It is safe because the seed only ever *creates*
missing codes — an installation that has already run it, renamed an account and
added its own is left completely alone.
"""

from django.db import migrations

from apps.accounting.chart import seed_chart_of_accounts


def forwards(apps, schema_editor):
    seed_chart_of_accounts(apps.get_model("accounting", "Account"))


def backwards(apps, schema_editor):
    """Deliberately does nothing.

    Unwinding this migration must not delete accounts. By the time anyone runs
    it there may be ledger entries pointing at them — and the ledger's PROTECT
    foreign key would refuse anyway, turning a rollback into a hard failure
    instead of a no-op.
    """


class Migration(migrations.Migration):
    dependencies = [
        ("accounting", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
