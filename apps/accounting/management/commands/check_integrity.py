"""``python manage.py check_integrity``

The nightly audit, run by Task Scheduler alongside the backup. Read-only — it
never corrects anything it finds, because a correction is a reversing entry
somebody decides to post, not a repair a scheduled job makes at 21:05 with
nobody watching.

Exit codes, because Task Scheduler shows the last result as a number and the
dashboard reads the stored run:

* ``0`` — everything balances.
* ``1`` — at least one check failed. The books disagree with themselves.

The result is written to an ``IntegrityRun`` row so the dashboard can show it
without re-running the checks on every page load — five aggregate queries over
the whole ledger is not something to do on the screen everybody lands on.
"""

from __future__ import annotations

import sys
import time

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.accounting.integrity import run_all


class Command(BaseCommand):
    help = "Check the ledger against itself: balances, orphans, stock, posted documents."

    def add_arguments(self, parser):
        parser.add_argument(
            "--quiet",
            action="store_true",
            help="Print only failures. What the nightly task uses.",
        )
        parser.add_argument(
            "--no-record",
            action="store_true",
            help="Do not write an IntegrityRun row. For a one-off look.",
        )

    def handle(self, *args, **options):
        started = time.monotonic()
        findings = run_all()
        duration_ms = int((time.monotonic() - started) * 1000)

        failures = [f for f in findings if not f.ok]

        for finding in findings:
            if options["quiet"] and finding.ok:
                continue
            style = self.style.SUCCESS if finding.ok else self.style.ERROR
            self.stdout.write(style(f"{finding.status:<4} {finding.name} — {finding.summary}"))
            for detail in finding.details:
                self.stdout.write(f"       {detail}")

        if not options["no_record"]:
            self._record(findings, duration_ms)

        if failures:
            self.stderr.write(
                self.style.ERROR(
                    f"\n{len(failures)} check(s) FAILED at "
                    f"{timezone.localtime():%d %b %Y %H:%M}.\n"
                    "\n"
                    "Nothing has been changed — this command only looks.\n"
                    "\n"
                    "The books disagree with themselves, which the application cannot\n"
                    "cause on its own: the ledger is append-only and every posting\n"
                    "balances inside its own transaction. The usual causes are a\n"
                    "half-finished restore, a database file that was copied while the\n"
                    "service was running, or a disk problem.\n"
                    "\n"
                    "What to do:\n"
                    "  1. Do not post anything else until this is resolved.\n"
                    "  2. Note the document codes listed above.\n"
                    "  3. Restore last night's backup onto a copy and run this again\n"
                    "     against it, to find out whether the problem is recent.\n"
                    "  4. Call whoever supports this system, with the list above."
                )
            )
            sys.exit(1)

        if not options["quiet"]:
            self.stdout.write(
                self.style.SUCCESS(f"\nAll {len(findings)} checks passed in {duration_ms} ms.")
            )

    # ------------------------------------------------------------------
    def _record(self, findings, duration_ms: int) -> None:
        from apps.accounting.models import IntegrityRun

        failures = [f for f in findings if not f.ok]
        report = "\n".join(
            f"{f.status} {f.name} — {f.summary}"
            + ("".join(f"\n    {d}" for d in f.details) if f.details else "")
            for f in findings
        )
        IntegrityRun.objects.create(
            ok=not failures,
            checks_run=len(findings),
            checks_failed=len(failures),
            duration_ms=duration_ms,
            report=report,
        )
