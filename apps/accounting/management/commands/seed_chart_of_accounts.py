"""Re-run the default chart of accounts seed.

Migration 0002 already seeds a fresh database, so this is for repair: someone
deleted a heading, or an installation predates an account being added to
``DEFAULT_CHART``. It is additive — nothing existing is renamed, re-parented or
removed.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.accounting.chart import seed_chart_of_accounts
from apps.accounting.models import Account


class Command(BaseCommand):
    help = "Create any default accounts that are missing. Never modifies existing ones."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be created and roll back.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        with transaction.atomic():
            created, existing = seed_chart_of_accounts(Account)
            if dry_run:
                transaction.set_rollback(True)

        self.stdout.write(
            self.style.SUCCESS(
                f"{'Would create' if dry_run else 'Created'} {created} account(s); "
                f"{existing} already present."
            )
        )
