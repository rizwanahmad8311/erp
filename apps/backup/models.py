"""Scheduled SQLite backup and restore for the Windows box.

No stored state yet — a backup is a file on disk, and when a run log arrives it
will inherit from ``apps.core.models.TimeStampedModel``. What is here now is the
pair of permissions the two actions need.
"""

from django.db import models


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
