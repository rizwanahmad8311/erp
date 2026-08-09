"""Backup and restore, including the whole round trip.

The round trip is the test that matters. Everything else here checks a piece;
``TestTheRoundTrip`` checks the only thing anybody actually cares about — that a
backup taken on Tuesday puts Tuesday back.

The wipe in that test **deletes the database file** rather than deleting rows.
That is both the more faithful disaster (a corrupt or lost SQLite file is what
happens; somebody running `DELETE FROM ledger_entry` is not) and the only way to
simulate it without writing a DELETE against an append-only table, which
CLAUDE.md §3 forbids outright — "not for a test fixture".
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
import zipfile
from pathlib import Path

import pytest
from django.core.management import call_command
from django.db import connection

from apps.backup import services
from apps.backup.models import BackupLog, Destination, Outcome
from apps.core.money import to_paisa

# `transaction=True` for the whole module, not as an optimisation but because
# `VACUUM INTO` cannot run inside a transaction — SQLite refuses outright. The
# default pytest-django fixture wraps each test in one and rolls it back, so a
# test of the real snapshot path has to opt out of it. Testing it any other way
# would be testing something that is not what production does.
pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def backup_root(settings, tmp_path):
    """Write archives into a temp directory, never into data/backups."""
    root = tmp_path / "backups"
    root.mkdir()
    settings.BACKUP_ROOT = root
    settings.BACKUP_USB_PATH = ""
    return root


# ---------------------------------------------------------------------------
# The archive
# ---------------------------------------------------------------------------
class TestTheArchive:
    def test_it_holds_the_database_the_media_and_a_manifest(self, backup_root, settings, tmp_path):
        media = tmp_path / "media"
        media.mkdir()
        (media / "logo.png").write_bytes(b"not really a png")
        settings.MEDIA_ROOT = media

        result = services.create_archive()

        with zipfile.ZipFile(result.archive) as zf:
            names = set(zf.namelist())
        assert services.DB_ARCHIVE_NAME in names
        assert services.MANIFEST_NAME in names
        assert "media/logo.png" in names

    def test_the_manifest_records_version_counts_and_a_checksum(self, backup_root):
        result = services.create_archive()

        with zipfile.ZipFile(result.archive) as zf:
            manifest = json.loads(zf.read(services.MANIFEST_NAME))

        assert manifest["app_version"]
        assert manifest["database_sha256"] == result.sha256
        assert len(manifest["database_sha256"]) == 64
        assert "auth.User" in manifest["row_counts"]
        dt.datetime.fromisoformat(manifest["created_at"])

    def test_the_checksum_is_of_the_database_not_of_the_zip(self, backup_root):
        """A zip records mtimes, so hashing it would give a different answer for
        the same data and `restore` could never verify anything."""
        import hashlib

        result = services.create_archive()

        with (
            zipfile.ZipFile(result.archive) as zf,
            zf.open(services.DB_ARCHIVE_NAME) as handle,
        ):
            digest = hashlib.sha256(handle.read()).hexdigest()

        assert digest == result.sha256

    def test_a_snapshot_is_taken_with_vacuum_into_not_a_file_copy(self, backup_root):
        """The live file is three files in WAL mode; copying it races the -wal.

        Proved by behaviour rather than by inspection: the snapshot opens as a
        standalone database with the schema in it, which a partial copy would
        not.
        """
        result = services.create_archive()
        import sqlite3
        import tempfile

        with zipfile.ZipFile(result.archive) as zf, tempfile.TemporaryDirectory() as tmp:
            zf.extract(services.DB_ARCHIVE_NAME, tmp)
            extracted = Path(tmp) / services.DB_ARCHIVE_NAME
            con = sqlite3.connect(extracted)
            tables = {
                row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            con.close()
        assert "auth_user" in tables

    def test_a_half_written_archive_is_never_left_behind(self, backup_root):
        services.create_archive()
        assert not list(backup_root.glob("*.partial"))
        assert not list(backup_root.glob(".staging-*"))


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------
class TestVerification:
    def test_a_good_archive_verifies(self, backup_root):
        result = services.create_archive()
        assert services.verify_archive(result.archive)["database_sha256"] == result.sha256

    def test_a_tampered_database_is_refused(self, backup_root, tmp_path):
        result = services.create_archive()

        # Rewrite the zip with a different database inside, keeping the manifest.
        with zipfile.ZipFile(result.archive) as zf:
            manifest = zf.read(services.MANIFEST_NAME)
        bad = tmp_path / "bad.zip"
        with zipfile.ZipFile(bad, "w") as zf:
            zf.writestr(services.MANIFEST_NAME, manifest)
            zf.writestr(services.DB_ARCHIVE_NAME, b"this is not the database")

        with pytest.raises(services.BackupError, match="damaged"):
            services.verify_archive(bad)

    def test_a_file_that_is_not_a_zip_says_so_in_words(self, tmp_path):
        junk = tmp_path / "erp-20260101-0000.zip"
        junk.write_bytes(b"not a zip at all")
        with pytest.raises(services.BackupError, match="not a readable zip"):
            services.verify_archive(junk)

    def test_a_missing_file_says_so(self, tmp_path):
        with pytest.raises(services.BackupError, match="No such backup"):
            services.verify_archive(tmp_path / "nope.zip")


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------
class TestRetention:
    def _make(self, root: Path, stamps: list[str]) -> list[Path]:
        paths = []
        for stamp in stamps:
            p = root / f"erp-{stamp}.zip"
            p.write_bytes(b"x")
            paths.append(p)
        return paths

    def test_it_keeps_the_newest_dailies(self, backup_root):
        stamps = [f"202608{day:02d}-2100" for day in range(1, 21)]
        self._make(backup_root, stamps)
        keep = services.select_for_retention(
            sorted(backup_root.glob("erp-*.zip")), daily=14, weekly=0, monthly=0
        )
        assert len(keep) == 14
        assert backup_root / "erp-20260820-2100.zip" in keep
        assert backup_root / "erp-20260801-2100.zip" not in keep

    def test_it_keeps_one_per_week_and_one_per_month_beyond_that(self, backup_root):
        # Every day for four months.
        stamps = []
        day = dt.date(2026, 4, 1)
        while day <= dt.date(2026, 7, 31):
            stamps.append(day.strftime("%Y%m%d-2100"))
            day += dt.timedelta(days=1)
        self._make(backup_root, stamps)

        keep = services.select_for_retention(
            sorted(backup_root.glob("erp-*.zip")), daily=14, weekly=8, monthly=12
        )
        kept_months = {p.stem[4:10] for p in keep}
        # Four calendar months are represented, not just the newest fortnight.
        assert kept_months == {"202604", "202605", "202606", "202607"}
        assert 14 < len(keep) < len(stamps)

    def test_counts_not_ages_so_an_idle_machine_keeps_its_backups(self, backup_root):
        """A machine switched off for a month still has fourteen dailies kept,
        rather than everything being older than the window and deleted."""
        stamps = [f"202601{day:02d}-2100" for day in range(1, 15)]
        self._make(backup_root, stamps)
        kept = services.select_for_retention(
            sorted(backup_root.glob("erp-*.zip")), daily=14, weekly=8, monthly=12
        )
        assert len(kept) == 14

    def test_prune_deletes_what_retention_does_not_keep(self, backup_root, settings):
        settings.BACKUP_KEEP_DAILY = 2
        settings.BACKUP_KEEP_WEEKLY = 0
        settings.BACKUP_KEEP_MONTHLY = 0
        self._make(backup_root, ["20260801-2100", "20260802-2100", "20260803-2100"])

        removed = services.prune()

        assert [p.name for p in removed] == ["erp-20260801-2100.zip"]
        assert len(list(backup_root.glob("erp-*.zip"))) == 2


# ---------------------------------------------------------------------------
# The USB copy
# ---------------------------------------------------------------------------
class TestTheUsbCopy:
    def test_an_absent_drive_warns_and_does_not_fail_the_run(self, backup_root, settings, tmp_path):
        settings.BACKUP_USB_PATH = str(tmp_path / "no-such-drive")
        result = services.create_archive()

        step = services.copy_to_usb(result.archive)

        assert step.outcome == Outcome.WARNING
        assert step.outcome != Outcome.FAILED
        assert "not there" in step.message

    def test_a_present_drive_gets_the_file(self, backup_root, settings, tmp_path):
        usb = tmp_path / "usb"
        usb.mkdir()
        settings.BACKUP_USB_PATH = str(usb)
        result = services.create_archive()

        step = services.copy_to_usb(result.archive)

        assert step.outcome == Outcome.OK
        assert (usb / result.archive.name).exists()

    def test_the_whole_run_still_succeeds_without_the_drive(self, backup_root, settings, tmp_path):
        settings.BACKUP_USB_PATH = str(tmp_path / "gone")
        result = services.run_backup(push=False)

        assert result.archive.exists()
        assert not result.failed
        assert result.warned


# ---------------------------------------------------------------------------
# rclone
# ---------------------------------------------------------------------------
class TestRclone:
    def test_a_missing_binary_gives_instructions_not_a_traceback(self, settings, monkeypatch):
        settings.BACKUP_RCLONE_BIN = "definitely-not-installed-rclone"
        monkeypatch.setattr(services.shutil, "which", lambda _name: None)

        ok, message = services.rclone_available()

        assert ok is False
        assert "rclone config" in message
        assert "rclone.org/downloads" in message
        # It must say the backup still happened without it.
        assert "still runs" in message

    def test_a_missing_remote_names_the_ones_that_exist(self, settings, monkeypatch):
        settings.BACKUP_RCLONE_REMOTE = "gdrive:erp"
        monkeypatch.setattr(services.shutil, "which", lambda _name: "/usr/bin/rclone")

        class Proc:
            returncode = 0
            stdout = "onedrive:\ndropbox:\n"
            stderr = ""

        monkeypatch.setattr(services.subprocess, "run", lambda *a, **k: Proc())

        ok, message = services.rclone_available()

        assert ok is False
        assert "no remote called 'gdrive'" in message
        assert "dropbox" in message and "onedrive" in message

    def test_a_failed_push_is_logged_as_failed_with_the_reason(
        self, backup_root, settings, monkeypatch
    ):
        monkeypatch.setattr(services, "rclone_available", lambda: (True, ""))

        class Proc:
            returncode = 1
            stdout = ""
            stderr = "couldn't connect: no such host"

        monkeypatch.setattr(services.subprocess, "run", lambda *a, **k: Proc())
        result = services.create_archive()

        step = services.push_to_drive(result.archive)

        assert step.outcome == Outcome.FAILED
        assert "no such host" in step.message

    def test_a_push_that_hangs_is_stopped_and_explained(self, backup_root, monkeypatch):
        import subprocess as sp

        monkeypatch.setattr(services, "rclone_available", lambda: (True, ""))

        def boom(*a, **k):
            raise sp.TimeoutExpired(cmd="rclone", timeout=1)

        monkeypatch.setattr(services.subprocess, "run", boom)
        result = services.create_archive()

        step = services.push_to_drive(result.archive)

        assert step.outcome == Outcome.FAILED
        assert "longer than" in step.message
        assert "hard disk" in step.message


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
class TestTheLog:
    def test_one_row_per_destination(self, backup_root, settings, tmp_path):
        settings.BACKUP_USB_PATH = str(tmp_path / "absent")
        services.run_backup(push=False)

        rows = BackupLog.objects.all()
        assert {r.destination for r in rows} == {Destination.LOCAL, Destination.USB}
        assert rows.get(destination=Destination.LOCAL).outcome == Outcome.OK
        assert rows.get(destination=Destination.USB).outcome == Outcome.WARNING

    def test_it_records_size_and_checksum(self, backup_root):
        result = services.run_backup(push=False, usb=False)
        row = BackupLog.objects.get(destination=Destination.LOCAL)
        assert row.sha256 == result.sha256
        assert row.size_bytes == result.size_bytes
        assert row.filename == result.archive.name

    def test_last_successful_backup_reads_the_local_copy(self, backup_root):
        assert services.last_successful_backup() is None
        services.run_backup(push=False, usb=False)
        assert services.last_successful_backup() is not None


# ---------------------------------------------------------------------------
# The restore guards
# ---------------------------------------------------------------------------
class TestRestoreRefuses:
    def test_it_refuses_while_the_service_is_up(self, backup_root, monkeypatch):
        from django.core.management.base import CommandError

        result = services.create_archive()
        monkeypatch.setattr(services, "service_is_running", lambda *a, **k: True)
        monkeypatch.setattr(
            "apps.backup.management.commands.restore.service_is_running", lambda *a, **k: True
        )

        with pytest.raises(CommandError, match="still running"):
            call_command("restore", str(result.archive), "--yes")

    def test_the_refusal_says_how_to_stop_it(self, backup_root, monkeypatch):
        from django.core.management.base import CommandError

        result = services.create_archive()
        monkeypatch.setattr(
            "apps.backup.management.commands.restore.service_is_running", lambda *a, **k: True
        )

        with pytest.raises(CommandError) as caught:
            call_command("restore", str(result.archive), "--yes")

        message = str(caught.value)
        assert "Ctrl+C" in message
        assert "python serve.py" in message

    def test_a_corrupt_archive_is_refused_before_anything_is_touched(self, tmp_path, monkeypatch):
        from django.core.management.base import CommandError

        junk = tmp_path / "erp-20260101-0000.zip"
        junk.write_bytes(b"nope")
        monkeypatch.setattr(
            "apps.backup.management.commands.restore.service_is_running", lambda *a, **k: False
        )

        with pytest.raises(CommandError):
            call_command("restore", str(junk), "--yes")


# ---------------------------------------------------------------------------
# The round trip — the one that matters
# ---------------------------------------------------------------------------
@pytest.fixture
def leaves_the_test_database_as_it_found_it(tmp_path):
    """Snapshot the test database, and put it back afterwards.

    The round-trip test deletes and replaces the database *file*, which is the
    whole point of it — but the file is the one every other test in the session
    shares. Without this, whatever runs next inherits a database that came from
    somewhere else, and the failure surfaces three files away with no obvious
    connection to backups.

    ``VACUUM INTO`` for the snapshot, and a plain file move to put it back:
    by teardown the test has already closed the connection, so there is nothing
    to be consistent with.
    """
    from apps.backup.services import database_path, vacuum_into

    keep = tmp_path / "session-db-snapshot.sqlite3"
    vacuum_into(keep)
    try:
        yield keep
    finally:
        live = database_path()
        connection.close()
        for suffix in ("-wal", "-shm"):
            stale = Path(str(live) + suffix)
            if stale.exists():
                stale.unlink()
        shutil.copyfile(keep, live)


@pytest.mark.usefixtures("leaves_the_test_database_as_it_found_it")
class TestTheRoundTrip:
    def test_seed_backup_wipe_restore_and_everything_comes_back(
        self, backup_root, settings, monkeypatch, tmp_path
    ):
        from apps.accounting.chart import seed_chart_of_accounts
        from apps.accounting.models import Account, LedgerEntry
        from apps.masters.models import Client, Item

        # ---------------------------------------------------------- seed
        seed_chart_of_accounts(Account)
        receivable = Account.objects.get(code="1130")
        sales = Account.objects.get(code="4100")

        shop = Client.objects.create(code="C-9001", name="Round Trip Traders")
        Item.objects.create(code="RT-1", name="Round Trip Rice 5kg")

        from tests.testapp.models import SampleDocument

        voucher = SampleDocument.objects.create(code="SI-2026-009001", party_name=shop.name)
        amount = to_paisa("12345.67")
        LedgerEntry.objects.create(
            account=receivable,
            debit_paisa=amount,
            credit_paisa=0,
            voucher_type="SampleDocument",
            voucher_id=voucher.pk,
            voucher_code=voucher.code,
            posting_date=dt.date(2026, 8, 9),
            remarks="round trip",
        )
        LedgerEntry.objects.create(
            account=sales,
            debit_paisa=0,
            credit_paisa=amount,
            voucher_type="SampleDocument",
            voucher_id=voucher.pk,
            voucher_code=voucher.code,
            posting_date=dt.date(2026, 8, 9),
            remarks="round trip",
        )

        def balances() -> dict:
            """A sample of balances, read from the ledger (CLAUDE.md §6)."""
            from django.db.models import Sum

            rows = LedgerEntry.objects.values("account__code").annotate(
                debit=Sum("debit_paisa"), credit=Sum("credit_paisa")
            )
            return {r["account__code"]: (r["debit"], r["credit"]) for r in rows}

        before_counts = services.row_counts()
        before_balances = balances()
        before_client = Client.objects.get(code="C-9001").name

        assert before_balances["1130"][0] >= amount
        # Debits equal credits before we start, so the assertion after the
        # restore is checking something real.
        total_debit = sum(d for d, _ in before_balances.values())
        total_credit = sum(c for _, c in before_balances.values())
        assert total_debit == total_credit

        # ---------------------------------------------------------- backup
        call_command("backup", "--no-usb")
        archive = next(iter(sorted(backup_root.glob("erp-*.zip"))))
        assert archive.exists()

        # ---------------------------------------------------------- wipe
        # Delete the database file itself. This is the disaster that actually
        # happens, and it avoids issuing a DELETE against the append-only
        # ledger, which CLAUDE.md §3 forbids even in a fixture.
        live = services.database_path()
        connection.close()
        for suffix in ("", "-wal", "-shm"):
            stale = Path(str(live) + suffix)
            if stale.exists():
                stale.unlink()
        call_command("migrate", verbosity=0, interactive=False, run_syncdb=True)

        assert Client.objects.filter(code="C-9001").count() == 0
        assert LedgerEntry.objects.count() == 0

        # ---------------------------------------------------------- restore
        monkeypatch.setattr(
            "apps.backup.management.commands.restore.service_is_running", lambda *a, **k: False
        )
        call_command("restore", str(archive), "--yes")

        # ---------------------------------------------------------- assert
        after_counts = services.row_counts()
        after_balances = balances()

        assert after_counts == before_counts, "row counts must match exactly"
        assert after_balances == before_balances, "every account balance must match exactly"
        assert Client.objects.get(code="C-9001").name == before_client
        assert LedgerEntry.objects.filter(voucher_code="SI-2026-009001").count() == 2

        # The safety copy of the wiped database exists, so restoring the wrong
        # night is undoable.
        assert list(backup_root.glob("erp-pre-restore-*.zip"))

    def test_restore_reports_the_counts_before_and_after(self, backup_root, monkeypatch, capsys):
        from apps.masters.models import Client

        Client.objects.create(code="C-9002", name="Reported Traders")
        call_command("backup", "--no-usb")
        archive = next(iter(sorted(backup_root.glob("erp-*.zip"))))

        monkeypatch.setattr(
            "apps.backup.management.commands.restore.service_is_running", lambda *a, **k: False
        )
        call_command("restore", str(archive), "--yes")

        out = capsys.readouterr().out
        assert "before" in out and "after" in out
        assert "masters.Client" in out
        assert "is intact" in out


# ---------------------------------------------------------------------------
# The screen
# ---------------------------------------------------------------------------
class TestTheScreen:
    def _login(self, client, django_user_model, *perms):
        from django.contrib.auth.models import Permission

        user = django_user_model.objects.create_user(username="ops", password="x")
        for perm in perms:
            app_label, codename = perm.split(".")
            user.user_permissions.add(
                Permission.objects.get(content_type__app_label=app_label, codename=codename)
            )
        client.force_login(user)
        return user

    def test_it_is_refused_without_run_backup(self, client, django_user_model):
        self._login(client, django_user_model)
        assert client.get("/backup/").status_code == 403

    def test_it_opens_with_run_backup(self, client, django_user_model, backup_root):
        self._login(client, django_user_model, "backup.run_backup")
        response = client.get("/backup/")
        assert response.status_code == 200
        assert b"Back up now" in response.content

    def test_a_stale_backup_is_shown_in_the_alarm_colour(
        self, client, django_user_model, backup_root, settings
    ):
        """Over BACKUP_STALE_HOURS the age goes rust — the same colour a
        cancelled document uses, because "there is no recent backup" is the same
        category of wrong."""
        from django.utils import timezone

        self._login(client, django_user_model, "backup.run_backup")
        services.run_backup(push=False, usb=False)
        row = BackupLog.objects.get(destination=Destination.LOCAL)
        BackupLog.objects.filter(pk=row.pk).update(
            created_at=timezone.now() - dt.timedelta(hours=settings.BACKUP_STALE_HOURS + 1)
        )

        body = client.get("/backup/").content.decode()

        assert "text-alarm" in body

    def test_a_fresh_backup_is_not_in_the_alarm_colour(
        self, client, django_user_model, backup_root
    ):
        self._login(client, django_user_model, "backup.run_backup")
        services.run_backup(push=False, usb=False)

        body = client.get("/backup/").content.decode()

        assert "Last backup" in body
        assert "font-semibold text-alarm" not in body

    def test_downloading_needs_restore_permission_not_just_run(
        self, client, django_user_model, backup_root
    ):
        """The archive is every price, every customer and every posting. Taking
        one is routine; walking out of the building with one is not."""
        result = services.run_backup(push=False, usb=False)
        self._login(client, django_user_model, "backup.run_backup")

        assert client.get(f"/backup/download/{result.archive.name}/").status_code == 403

    def test_a_holder_of_restore_backup_can_download(self, client, django_user_model, backup_root):
        result = services.run_backup(push=False, usb=False)
        self._login(client, django_user_model, "backup.restore_backup")

        response = client.get(f"/backup/download/{result.archive.name}/")

        assert response.status_code == 200
        assert response["Content-Disposition"].startswith("attachment")

    def test_the_download_cannot_walk_out_of_the_backup_directory(
        self, client, django_user_model, backup_root
    ):
        self._login(client, django_user_model, "backup.restore_backup")
        for attempt in ("..%2F..%2Fdata%2Ferp.sqlite3", "....//erp.sqlite3"):
            assert client.get(f"/backup/download/{attempt}/").status_code in (404, 400)
