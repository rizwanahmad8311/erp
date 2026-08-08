"""Add the two cheque accounts to an existing chart.

``1160 Cheques in Hand`` and ``2140 Cheques Issued`` arrived with
:mod:`apps.payments`. A cheque is not money until the bank says so, and posting
one straight to Bank overstates the balance for however many weeks a post-dated
cheque is held — which in this business is most of them.

This re-runs the same seed as migration 0002. The seed only ever *creates*
missing codes, so an installation that has renamed accounts or added its own is
left exactly as it is; all this does is fill in the two that are new.
"""

from django.db import migrations

from apps.accounting.chart import seed_chart_of_accounts


def forwards(apps, schema_editor):
    seed_chart_of_accounts(apps.get_model("accounting", "Account"))


def backwards(apps, schema_editor):
    """Deliberately does nothing.

    Same reasoning as migration 0002: by the time anyone unwinds this there may
    be ledger entries pointing at these accounts, and the ledger's PROTECT
    foreign key would refuse — turning a rollback into a hard failure rather
    than a no-op.
    """


class Migration(migrations.Migration):
    dependencies = [
        ("accounting", "0003_stock_ledger"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
