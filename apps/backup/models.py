"""Scheduled SQLite backup and restore for the Windows box.

Two models: a permission holder with no table, and a run log with one.
"""

from __future__ import annotations

from django.db import models

from apps.core.models import TimeStampedModel


class BackupPolicy(models.Model):
    """A permission holder. **No table, no rows, nothing stored.**

    Django attaches every permission to a model, and "may this person restore
    the database" is not about any model's rows — it is about the file the whole
    installation lives in. ``managed = False`` is the standard way to say that:
    ``makemigrations`` records the model, ``migrate`` creates no table, and the
    ``post_migrate`` hook still creates the content type and the permissions,
    which is the only thing this class is for.

    ``default_permissions = ()`` drops the four Django would otherwise add.
    ``add_backuppolicy`` on a model with no rows would be a permission that
    means nothing and that somebody would eventually grant thinking it did.
    """

    class Meta:
        managed = False
        default_permissions = ()
        verbose_name = "backup"
        verbose_name_plural = "backup"
        permissions = [
            ("run_backup", "Can take a backup of the database"),
            # Deliberately separate from run_backup. Taking a copy is safe and
            # routine; writing one back destroys every document posted since it
            # was taken, and there is no reversing entry for that.
            ("restore_backup", "Can restore the database from a backup, overwriting it"),
        ]

    def __str__(self) -> str:  # pragma: no cover - never instantiated
        return "Backup"


class Destination(models.TextChoices):
    """Where a copy was being put.

    Three destinations, logged separately rather than as one row with three
    flags, because they fail independently and for different reasons: the USB
    drive is unplugged, the Drive push has no internet, the local disk is full.
    A single "backup failed" row cannot say which, and the answer is the whole
    difference between "plug the stick back in" and "call somebody".
    """

    LOCAL = "LOCAL", "Local disk"
    USB = "USB", "USB drive"
    DRIVE = "DRIVE", "Google Drive"


class Outcome(models.TextChoices):
    """How it went.

    ``WARNING`` is its own outcome and is load-bearing. An absent USB drive must
    not fail the run — the archive is written and the Drive push still has to
    happen — but it must not read as success either, or a stick that has been
    out of the machine for a month looks exactly like one that is working.
    """

    OK = "OK", "Succeeded"
    WARNING = "WARNING", "Completed with a warning"
    FAILED = "FAILED", "Failed"


class BackupLog(TimeStampedModel):
    """One row per destination attempt.

    Not per *run*: a run writes the archive locally, copies it to a USB drive
    and pushes it to Drive, and those are three things that can each go their
    own way. ``run_id`` ties them back together for the history screen.

    This table is the only place the backup system keeps state. The archives
    themselves are files, deliberately — a backup you can only find through the
    application is a backup you cannot use on the morning the application will
    not start.
    """

    #: Groups the rows written by a single `backup` invocation. A timestamp
    #: string rather than a foreign key: there is no "run" table, and inventing
    #: one to hold three rows would be a table that exists to be joined.
    run_id = models.CharField(
        max_length=32,
        db_index=True,
        help_text="The archive stamp, e.g. 20260809-2100. Shared by the rows of one run.",
    )
    destination = models.CharField(max_length=8, choices=Destination.choices)
    outcome = models.CharField(max_length=8, choices=Outcome.choices)

    filename = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Archive name, without a directory. The directory differs per destination.",
    )
    size_bytes = models.BigIntegerField(
        default=0,
        help_text="Size of the archive. Zero when the attempt never got as far as a file.",
    )
    #: Of the **database file inside the archive**, not of the archive itself.
    #: A zip is not byte-stable — it records mtimes — so hashing the zip would
    #: give a different answer for the same data. `restore` checks this one.
    sha256 = models.CharField(max_length=64, blank=True, default="")

    target = models.CharField(
        max_length=500,
        blank=True,
        default="",
        help_text="Where it was written: a directory, or an rclone remote.",
    )
    message = models.TextField(
        blank=True,
        default="",
        help_text="Why it failed or warned, in words somebody can act on.",
    )
    duration_ms = models.IntegerField(default=0)

    class Meta:
        verbose_name = "backup log"
        verbose_name_plural = "backup log"
        ordering = ["-created_at", "destination"]
        indexes = [
            models.Index(fields=["-created_at"], name="backuplog_recent_idx"),
            models.Index(fields=["outcome", "-created_at"], name="backuplog_outcome_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.run_id} {self.destination} {self.outcome}"

    @property
    def succeeded(self) -> bool:
        return self.outcome == Outcome.OK

    @property
    def summary(self) -> str:
        """The first line of ``message`` — what the history table shows.

        The messages are written as instructions, several paragraphs long, so
        that a person following them has everything. Repeating all of that under
        every failed row — and there will be one a night until somebody fixes
        it — buries the history it is meant to annotate. The first line says
        *what happened*; the panel above the table carries the *what to do*.
        """
        for line in self.message.splitlines():
            if line.strip():
                return line.strip()
        return ""

    @property
    def size_display(self) -> str:
        """Human-sized, for the history table. Never used in a calculation."""
        size = float(self.size_bytes)
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024 or unit == "GB":
                return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} GB"  # pragma: no cover - unreachable, the loop returns


__all__ = ["BackupLog", "BackupPolicy", "Destination", "Outcome"]
