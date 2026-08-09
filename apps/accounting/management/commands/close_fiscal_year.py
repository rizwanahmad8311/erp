"""``python manage.py close_fiscal_year 2026 [--dry-run]``

Brings every income and expense account to zero and carries the year's profit to
Retained Earnings, so next year starts from nothing.

Run it **once**, after the accountant has finished with the year and not before.
It is reversible — the close is a document like any other and cancelling it
writes the mirror entries — but reversing a year-end after three weeks of the
next year have been posted is an afternoon nobody wants.

``--dry-run`` prints the exact same plan the real run posts, account by account,
and writes nothing. It is not a second implementation: both call
:func:`apps.accounting.yearend.build_plan`, so what you read is what you get.

On document numbering: there is nothing to reset. Sequences are keyed by
``(prefix, fiscal_year)``, so SI-2027-000001 is already the first invoice of
2027 whether or not 2026 was ever closed (CLAUDE.md §5). This command
deliberately does not touch ``DocumentSequence`` — hand-editing ``last_number``
is exactly what the read-only admin exists to prevent.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.accounting.models import FiscalYearClose
from apps.accounting.yearend import build_plan
from apps.core.exceptions import CoreError
from apps.core.money import fmt


class Command(BaseCommand):
    help = "Post the year-end closing entries for a fiscal year."

    def add_arguments(self, parser):
        parser.add_argument("fiscal_year", type=int, help="The year to close, e.g. 2026.")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print exactly what would be posted, and write nothing.",
        )
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Do not ask for confirmation. For scripted use.",
        )

    def handle(self, *args, **options):
        year = options["fiscal_year"]
        dry_run = options["dry_run"]

        # Asked before the plan, because a year that has already been closed has
        # a P&L of zero by definition — so "nothing to close" would be the
        # message, and it would be true and useless. "2026 was closed by
        # YC-2026-000001" is what the person needs to hear.
        already = FiscalYearClose.objects.live().filter(fiscal_year=year).first()
        if already is not None:
            raise CommandError(
                f"{year} has already been closed by {already.code} ({already.status}).\n"
                f"To close it again, cancel {already.code} first — that writes the "
                f"reversing entries and reopens the year."
            )

        plan = build_plan(year)
        self._print_plan(plan)

        if plan.is_empty:
            self.stdout.write(
                self.style.WARNING(
                    f"\nNothing to close: no income or expense account moved in {year}."
                )
            )
            return

        if dry_run:
            self.stdout.write(
                self.style.SUCCESS("\nDry run — nothing was written. Re-run without --dry-run.")
            )
            return

        if not options["yes"]:
            self.stdout.write(
                self.style.WARNING(
                    f"\nThis posts the entries above and closes {year}.\n"
                    "It can be reversed afterwards, but not quietly."
                )
            )
            if input("\nType 'yes' to close the year: ").strip().lower() != "yes":
                self.stdout.write("Nothing was changed.")
                return

        from apps.accounting.services import create_fiscal_year_close, post_fiscal_year_close

        try:
            with transaction.atomic():
                close = create_fiscal_year_close(fiscal_year=year)
                post_fiscal_year_close(close)
        except CoreError as exc:
            # A business refusal: the year is already closed, or there is
            # nothing in it. Printed as a sentence, never as a traceback.
            raise CommandError(str(exc)) from exc

        self.stdout.write(
            self.style.SUCCESS(
                f"\n{year} closed by {close.code}. "
                f"{'Profit' if close.profit_paisa >= 0 else 'Loss'} of "
                f"{fmt(abs(close.profit_paisa))} carried to Retained Earnings."
            )
        )
        self.stdout.write(
            "\nTo reverse this, cancel it from the accounting admin — it writes the\n"
            "mirror entries like any other cancellation. Do it before posting into\n"
            "the new year if you can."
        )

    # ------------------------------------------------------------------
    def _print_plan(self, plan) -> None:
        self.stdout.write(
            f"Closing {plan.fiscal_year} — {plan.period_from:%d %b %Y} to {plan.period_to:%d %b %Y}"
        )
        if plan.is_empty:
            return

        self.stdout.write("\n  account                                    debit        credit")
        for balance in plan.balances:
            net = balance.net_paisa
            debit = fmt(-net) if net < 0 else ""
            credit = fmt(net) if net > 0 else ""
            label = f"{balance.account.code} {balance.account.name}"
            self.stdout.write(f"  {label:<40} {debit:>12} {credit:>13}")

        profit = plan.profit_paisa
        label = f"{plan.retained_earnings.code} {plan.retained_earnings.name}"
        debit = fmt(-profit) if profit < 0 else ""
        credit = fmt(profit) if profit > 0 else ""
        self.stdout.write(f"  {label:<40} {debit:>12} {credit:>13}")

        self.stdout.write("")
        self.stdout.write(f"  income   {fmt(plan.income_paisa):>12}")
        self.stdout.write(f"  expenses {fmt(plan.expense_paisa):>12}")
        self.stdout.write(
            self.style.SUCCESS(f"  {'profit' if profit >= 0 else 'LOSS':<8} {fmt(abs(profit)):>12}")
        )
