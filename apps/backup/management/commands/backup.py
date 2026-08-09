"""``python manage.py backup [--push] [--no-usb]``

What Windows Task Scheduler runs at 21:00 every night, and what the
"Back up now" button on the admin screen calls.

Output is written for somebody who is not a developer and who is reading it
because something is wrong. Every failure says what happened and what to do
next; none of them is a traceback.

Exit codes, because Task Scheduler shows the last result as a number:

* ``0`` — everything worked, or the only problems were warnings (an unplugged
  USB drive is a warning, and a nightly task that goes red for that is a task
  people stop believing).
* ``1`` — a copy that was asked for did not happen.
* ``2`` — no backup was taken at all.
"""

from __future__ import annotations

import sys

from django.core.management.base import BaseCommand

from apps.backup.models import Destination, Outcome
from apps.backup.services import BackupError, run_backup

#: Exit codes. Named, because `sys.exit(2)` in three places is three magic
#: numbers that drift from the docstring.
EXIT_OK = 0
EXIT_COPY_FAILED = 1
EXIT_NO_BACKUP = 2


class Command(BaseCommand):
    help = "Take a backup of the database and media, optionally pushing it to Google Drive."

    def add_arguments(self, parser):
        parser.add_argument(
            "--push",
            action="store_true",
            help="Also upload the archive to Google Drive with rclone.",
        )
        parser.add_argument(
            "--no-usb",
            action="store_true",
            help="Skip the USB copy even when BACKUP_USB_PATH is set.",
        )

    def handle(self, *args, **options):
        try:
            result = run_backup(push=options["push"], usb=not options["no_usb"])
        except BackupError as exc:
            # The archive itself could not be written. Nothing downstream can
            # help, because everything downstream copies this file.
            self.stderr.write(self.style.ERROR("BACKUP FAILED — no copy was taken.\n"))
            self.stderr.write(str(exc))
            sys.exit(EXIT_NO_BACKUP)

        self.stdout.write(self.style.SUCCESS(f"Backup {result.run_id} written."))
        self.stdout.write(f"  file    {result.archive}")
        self.stdout.write(f"  size    {result.size_bytes / 1024 / 1024:.1f} MB")
        self.stdout.write(f"  sha256  {result.sha256}")

        for step in result.steps:
            if step.destination == Destination.LOCAL:
                continue
            label = Destination(step.destination).label
            if step.outcome == Outcome.OK:
                self.stdout.write(self.style.SUCCESS(f"  {label}: copied to {step.target}"))
            elif step.outcome == Outcome.WARNING:
                self.stdout.write(self.style.WARNING(f"  {label}: skipped"))
                self.stdout.write(f"    {step.message}")
            else:
                self.stderr.write(self.style.ERROR(f"  {label}: FAILED"))
                self.stderr.write(f"    {step.message}")

        if result.pruned:
            self.stdout.write(
                f"  pruned  {len(result.pruned)} old backup(s) beyond the retention policy"
            )

        if result.failed:
            self.stderr.write(
                self.style.ERROR(
                    "\nThe backup was taken and is on the hard disk, but a copy that "
                    "was asked for did not happen. See above."
                )
            )
            sys.exit(EXIT_COPY_FAILED)

        # No sys.exit on success: returning normally is already exit code 0, and
        # raising SystemExit would make `call_command("backup")` blow up in any
        # caller that is not a shell — the admin screen and the tests both are.
