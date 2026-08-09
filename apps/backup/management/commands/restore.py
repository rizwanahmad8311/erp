"""``python manage.py restore <file.zip>``

Putting a backup back. The dangerous one, so it argues before it acts.

Four refusals stand between an operator and a bad afternoon:

1. **The checksum does not match.** Checked before anything is touched, because
   restoring a corrupt archive over a working installation is the one mistake
   here with no way back.
2. **The service is still running.** Replacing the database under a live
   waitress fails outright on Windows and silently half-works elsewhere.
3. **Nobody typed yes.** Unless ``--yes`` was passed, which is what the tests
   and any future scripted use pass.
4. It always takes a snapshot of what is there now first — not a refusal, but
   the same instinct. That file is the undo for "I restored the wrong night".
"""

from __future__ import annotations

import sys
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.backup.services import BackupError, restore_archive, service_is_running, verify_archive


class Command(BaseCommand):
    help = "Restore the database and media from a backup archive, overwriting the current one."

    def add_arguments(self, parser):
        parser.add_argument("archive", help="Path to the erp-YYYYMMDD-HHMM.zip file.")
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Do not ask for confirmation. For scripted use.",
        )
        parser.add_argument(
            "--verify-only",
            action="store_true",
            help="Check the archive's checksum and contents, then stop without restoring.",
        )

    def handle(self, *args, **options):
        archive = Path(options["archive"]).expanduser()

        # 1. Verify first, always, before the service check even — a bad file is
        #    worth knowing about whether or not the service happens to be up.
        try:
            manifest = verify_archive(archive)
        except BackupError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(self.style.SUCCESS(f"{archive.name} is intact."))
        self.stdout.write(f"  taken       {manifest.get('created_at', 'unknown')}")
        self.stdout.write(f"  app version {manifest.get('app_version', 'unknown')}")
        self.stdout.write(f"  sha256      {manifest.get('database_sha256', '')}")

        counts = manifest.get("row_counts", {})
        if counts:
            self.stdout.write("  it contains:")
            for label, count in sorted(counts.items()):
                self.stdout.write(f"    {count:>9,}  {label}")

        if options["verify_only"]:
            return

        # 2. Refuse while the service is up.
        if service_is_running():
            raise CommandError(
                "The ERP is still running, so the database file is in use.\n"
                "\n"
                "Stop it first:\n"
                "  - Find the black window titled 'ERP serving on ...' and press Ctrl+C,\n"
                "    or close it.\n"
                "  - If it runs as a scheduled task, open Task Scheduler and end it.\n"
                "\n"
                "Then run this command again. Start the ERP back up afterwards with:\n"
                "  python serve.py"
            )

        # 3. Say plainly what is about to be destroyed.
        if not options["yes"]:
            self.stdout.write(
                self.style.WARNING(
                    "\nThis REPLACES the current database and media folder.\n"
                    "Everything entered since this backup was taken will be gone.\n"
                    "A copy of the current database is saved first, so this can be undone."
                )
            )
            if input("\nType 'yes' to restore: ").strip().lower() != "yes":
                self.stdout.write("Nothing was changed.")
                sys.exit(0)

        try:
            outcome = restore_archive(archive)
        except BackupError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(self.style.SUCCESS("\nRestored."))
        self.stdout.write(f"  the database as it was is saved at {outcome['safety_archive']}")

        # 4. Row counts before and after, so the operator can see it landed.
        before, after = outcome["before"], outcome["after"]
        expected = outcome["expected"]
        self.stdout.write("\n  table                              before      after   in backup")
        mismatched = []
        for label in sorted(set(before) | set(after)):
            want = expected.get(label)
            got = after.get(label, 0)
            self.stdout.write(
                f"  {label:<30} {before.get(label, 0):>10,} {got:>10,} "
                f"{'' if want is None else format(want, ',>10')}"
            )
            if want is not None and want != got:
                mismatched.append(label)

        if mismatched:
            # Not necessarily wrong — `migrate` may legitimately have added rows
            # for a schema change — but it is the thing to look at first.
            self.stdout.write(
                self.style.WARNING(
                    "\n  These differ from what the manifest recorded: "
                    + ", ".join(mismatched)
                    + "\n  Migrations run after a restore, so a newer build can explain a "
                    "difference here. Anything else is worth checking."
                )
            )

        self.stdout.write("\nStart the ERP again with:  python serve.py")
