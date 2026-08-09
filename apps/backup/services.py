"""Taking a copy of the installation, and putting one back.

The whole installation is one SQLite file plus one media folder, and both
constraints in the brief point the same way: no internet on the box, and the
person doing this at 9pm on a Saturday is not a developer. So:

* **`VACUUM INTO`, never a file copy.** SQLite in WAL mode is three files, and
  the data you want is spread across the database and its `-wal` companion at
  any given instant. Copying `erp.sqlite3` while waitress is serving gets you a
  file that opens, passes a cursory look, and is missing the last few
  transactions — the worst possible failure, because nobody notices until they
  restore it. `VACUUM INTO` asks SQLite itself for a consistent snapshot, takes
  a read lock rather than blocking writers out, and produces a defragmented
  single file. It is the only supported way to back up a live database without
  stopping it.
* **A zip with a manifest.** The archive holds the database, the media folder
  and a `manifest.json` naming what is inside, when it was taken, which build
  wrote it, the row counts and a SHA-256 of the database file. `restore` checks
  that hash before it touches anything.
* **Every attempt is logged**, per destination, because the three destinations
  fail for three different reasons and "backup failed" does not tell anyone
  which.

Nothing here runs inside a posting transaction (CLAUDE.md §4) — it is all I/O,
and the SQLite write lock is taken at ``BEGIN``.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import socket
import subprocess
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from django.conf import settings
from django.db import connection
from django.utils import timezone

from .models import BackupLog, Destination, Outcome

#: Name of the database inside the archive. Fixed, so `restore` knows what to
#: look for without reading the manifest first.
DB_ARCHIVE_NAME = "erp.sqlite3"
MANIFEST_NAME = "manifest.json"
MEDIA_PREFIX = "media/"

#: What goes in the manifest's row counts. Not every table — a count of
#: `django_session` tells nobody anything — but every table whose row count
#: somebody would actually check after a restore.
COUNTED_MODELS = (
    "accounting.LedgerEntry",
    "accounting.StockEntry",
    "accounting.Account",
    "masters.Item",
    "masters.Client",
    "masters.Vendor",
    "sales.SalesInvoice",
    "sales.SalesInvoiceLine",
    "sales.SalesReturn",
    "purchasing.PurchaseInvoice",
    "purchasing.PurchaseInvoiceLine",
    "payments.Payment",
    "auth.User",
)


class BackupError(Exception):
    """Something went wrong that the operator has to know about.

    Carries a message written for somebody who is not a developer: what
    happened, and what to do about it.
    """


@dataclass
class StepResult:
    """One destination attempt, ready to be logged."""

    destination: str
    outcome: str
    message: str = ""
    target: str = ""
    duration_ms: int = 0


@dataclass
class RunResult:
    """Everything one `backup` invocation did."""

    run_id: str
    archive: Path | None = None
    sha256: str = ""
    size_bytes: int = 0
    steps: list[StepResult] = field(default_factory=list)
    pruned: list[str] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return any(s.outcome == Outcome.FAILED for s in self.steps)

    @property
    def warned(self) -> bool:
        return any(s.outcome == Outcome.WARNING for s in self.steps)


# ---------------------------------------------------------------------------
# Reading the database
# ---------------------------------------------------------------------------
def database_path() -> Path:
    """The live SQLite file."""
    return Path(connection.settings_dict["NAME"])


def sha256_of(path: Path) -> str:
    """Streamed, because the database will not always fit comfortably in RAM."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def row_counts() -> dict[str, int]:
    """Row counts per major table, for the manifest.

    A missing model is skipped rather than raising: the manifest is a record,
    and a backup that refuses to run because this list drifted from the models
    would be a backup that stops happening.
    """
    from django.apps import apps as django_apps

    counts: dict[str, int] = {}
    for label in COUNTED_MODELS:
        try:
            model = django_apps.get_model(label)
        except LookupError:  # pragma: no cover - only if a model is renamed
            continue
        counts[label] = model._base_manager.count()
    return counts


def vacuum_into(target: Path) -> None:
    """Ask SQLite for a consistent snapshot at ``target``.

    ``VACUUM INTO`` refuses to overwrite, so the caller must hand a path that
    does not exist yet.

    This is deliberately not wrapped in ``transaction.atomic()``: ``VACUUM``
    cannot run inside a transaction, and Django's autocommit is what lets it
    through.
    """
    if target.exists():  # pragma: no cover - callers use a fresh temp dir
        raise BackupError(f"{target} already exists; VACUUM INTO will not overwrite it.")
    target.parent.mkdir(parents=True, exist_ok=True)
    with connection.cursor() as cursor:
        # Parameter substitution is not available for VACUUM INTO in SQLite,
        # so the path is quoted as a SQL string literal with '' escaping. The
        # path comes from settings and a tempdir, never from user input.
        literal = str(target).replace("'", "''")
        cursor.execute(f"VACUUM INTO '{literal}'")


# ---------------------------------------------------------------------------
# Making the archive
# ---------------------------------------------------------------------------
def _stamp(when=None) -> str:
    return (when or timezone.localtime()).strftime("%Y%m%d-%H%M")


def create_archive(*, run_id: str | None = None, destination_dir: Path | None = None) -> RunResult:
    """Snapshot the database, zip it with the media folder, write the manifest.

    Returns a :class:`RunResult` whose ``archive`` is the finished zip. Raises
    :class:`BackupError` with an actionable message if the snapshot itself
    fails — that is the one failure there is no working around, because
    everything downstream copies the file this produces.
    """
    run_id = run_id or _stamp()
    out_dir = Path(destination_dir or settings.BACKUP_ROOT)
    out_dir.mkdir(parents=True, exist_ok=True)
    archive = out_dir / f"erp-{run_id}.zip"

    started = time.monotonic()
    staging = out_dir / f".staging-{run_id}"
    staging.mkdir(parents=True, exist_ok=True)
    snapshot = staging / DB_ARCHIVE_NAME

    try:
        try:
            vacuum_into(snapshot)
        except Exception as exc:  # re-raised as an operator message
            raise BackupError(
                f"Could not snapshot the database: {exc}\n"
                f"Check there is free disk space on the drive holding {out_dir}."
            ) from exc

        digest = sha256_of(snapshot)
        manifest = {
            "created_at": timezone.now().isoformat(),
            "run_id": run_id,
            "app_version": settings.APP_VERSION,
            "database_name": DB_ARCHIVE_NAME,
            "database_sha256": digest,
            "database_bytes": snapshot.stat().st_size,
            "row_counts": row_counts(),
        }

        # Written to a temporary name and moved into place, so a run that dies
        # halfway does not leave a half-zip that looks like a backup.
        partial = archive.with_suffix(".zip.partial")
        with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(MANIFEST_NAME, json.dumps(manifest, indent=2, sort_keys=True))
            zf.write(snapshot, DB_ARCHIVE_NAME)
            media_root = Path(settings.MEDIA_ROOT)
            if media_root.is_dir():
                for item in sorted(media_root.rglob("*")):
                    if item.is_file():
                        zf.write(item, MEDIA_PREFIX + str(item.relative_to(media_root)))
        partial.replace(archive)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    return RunResult(
        run_id=run_id,
        archive=archive,
        sha256=digest,
        size_bytes=archive.stat().st_size,
        steps=[
            StepResult(
                destination=Destination.LOCAL,
                outcome=Outcome.OK,
                target=str(out_dir),
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        ],
    )


def read_manifest(archive: Path) -> dict:
    """The manifest out of an archive, or a :class:`BackupError` saying why not."""
    if not archive.is_file():
        raise BackupError(f"No such backup file: {archive}")
    try:
        with zipfile.ZipFile(archive) as zf, zf.open(MANIFEST_NAME) as handle:
            return json.load(handle)
    except KeyError as exc:
        raise BackupError(
            f"{archive.name} has no {MANIFEST_NAME} inside it. "
            "It was not written by this system, or it is truncated."
        ) from exc
    except zipfile.BadZipFile as exc:
        raise BackupError(
            f"{archive.name} is not a readable zip file — it is corrupt or was "
            "copied while it was still being written."
        ) from exc


def verify_archive(archive: Path) -> dict:
    """Check the database inside the archive against the manifest's SHA-256.

    This is what stands between a bad file and an overwritten installation, so
    it runs **before** `restore` touches anything at all — including before it
    takes the safety copy.
    """
    manifest = read_manifest(archive)
    expected = manifest.get("database_sha256", "")
    if not expected:
        raise BackupError(f"{archive.name} has a manifest with no checksum in it.")

    digest = hashlib.sha256()
    with zipfile.ZipFile(archive) as zf:
        try:
            entry = zf.open(manifest.get("database_name", DB_ARCHIVE_NAME))
        except KeyError as exc:
            raise BackupError(f"{archive.name} has a manifest but no database inside.") from exc
        with entry as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)

    if digest.hexdigest() != expected:
        raise BackupError(
            f"{archive.name} is damaged: the database inside does not match the "
            "checksum recorded when it was taken.\n"
            f"  expected {expected}\n"
            f"  found    {digest.hexdigest()}\n"
            "Do not restore this file. Use an older backup."
        )
    return manifest


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------
def _archives(root: Path) -> list[Path]:
    return sorted(root.glob("erp-*.zip"))


def select_for_retention(
    archives: list[Path], *, daily: int, weekly: int, monthly: int
) -> set[Path]:
    """Which archives to keep, grandfather-father-son.

    Counts rather than ages, deliberately. "Delete anything older than fourteen
    days" on a machine that was switched off for three weeks deletes every
    backup it has; "keep the newest fourteen days that exist" keeps fourteen.

    An archive is kept if it is one of the newest ``daily``, **or** it is the
    newest in its ISO week for the newest ``weekly`` weeks, **or** it is the
    newest in its month for the newest ``monthly`` months.
    """
    if not archives:
        return set()

    def stamp(path: Path) -> str:
        # erp-20260809-2100.zip -> 20260809-2100
        return path.stem[len("erp-") :]

    ordered = sorted(archives, key=stamp, reverse=True)
    keep: set[Path] = set(ordered[:daily])

    def newest_per(bucket_of) -> list[Path]:
        seen: dict[str, Path] = {}
        for path in ordered:  # newest first, so the first of each bucket wins
            key = bucket_of(stamp(path))
            seen.setdefault(key, path)
        return list(seen.values())

    # 20260809-2100 -> ISO year+week, and -> year+month
    import datetime as dt

    def as_date(s: str) -> dt.date:
        return dt.datetime.strptime(s[:8], "%Y%m%d").date()

    weeks = newest_per(lambda s: "{}-{}".format(*as_date(s).isocalendar()[:2]))
    months = newest_per(lambda s: as_date(s).strftime("%Y-%m"))

    keep |= set(sorted(weeks, key=stamp, reverse=True)[:weekly])
    keep |= set(sorted(months, key=stamp, reverse=True)[:monthly])
    return keep


def prune(root: Path | None = None) -> list[Path]:
    """Delete the archives retention does not keep. Returns what went."""
    root = Path(root or settings.BACKUP_ROOT)
    if not root.is_dir():
        return []
    archives = _archives(root)
    keep = select_for_retention(
        archives,
        daily=settings.BACKUP_KEEP_DAILY,
        weekly=settings.BACKUP_KEEP_WEEKLY,
        monthly=settings.BACKUP_KEEP_MONTHLY,
    )
    removed = []
    for path in archives:
        if path not in keep:
            path.unlink()
            removed.append(path)
    return removed


# ---------------------------------------------------------------------------
# Copies: USB, and Google Drive through rclone
# ---------------------------------------------------------------------------
def copy_to_usb(archive: Path, *, target: str | None = None) -> StepResult:
    """Second local copy, intended for a USB stick.

    **An absent drive is a warning, never a failure.** The stick spends most of
    its life out of the machine; if that stopped the run, the Drive push — the
    copy that survives the building burning down — would stop with it.
    """
    started = time.monotonic()
    target = settings.BACKUP_USB_PATH if target is None else target
    if not target:
        return StepResult(
            destination=Destination.USB,
            outcome=Outcome.WARNING,
            message="No USB path configured. Set BACKUP_USB_PATH in .env to enable it.",
        )

    dest = Path(target)
    if not dest.is_dir():
        return StepResult(
            destination=Destination.USB,
            outcome=Outcome.WARNING,
            target=str(dest),
            message=(
                f"The USB drive is not there ({dest}). The backup was still written "
                "to the hard disk and pushed to Google Drive. Plug the drive in and "
                "run the backup again if you want a copy on it."
            ),
        )

    try:
        shutil.copy2(archive, dest / archive.name)
    except OSError as exc:
        return StepResult(
            destination=Destination.USB,
            outcome=Outcome.WARNING,
            target=str(dest),
            message=(
                f"Could not write to the USB drive ({exc.strerror or exc}). "
                "It may be full or write-protected. The other copies were made."
            ),
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    return StepResult(
        destination=Destination.USB,
        outcome=Outcome.OK,
        target=str(dest),
        duration_ms=int((time.monotonic() - started) * 1000),
    )


def rclone_available() -> tuple[bool, str]:
    """Is rclone installed and does it have the configured remote?

    Returns ``(ok, message)``. The message is the instruction to print — the
    whole point of this function is that a missing rclone produces a sentence
    somebody can act on rather than a ``FileNotFoundError`` traceback.
    """
    binary = settings.BACKUP_RCLONE_BIN
    remote = settings.BACKUP_RCLONE_REMOTE
    remote_name = remote.split(":", 1)[0]

    if shutil.which(binary) is None:
        return False, (
            f"rclone is not installed (looked for '{binary}').\n"
            "\n"
            "To set it up, once, on a machine with internet:\n"
            "  1. Download rclone for Windows from https://rclone.org/downloads/\n"
            "  2. Unzip it and put rclone.exe somewhere on the PATH,\n"
            "     or set BACKUP_RCLONE_BIN in .env to its full path.\n"
            f"  3. Run:  rclone config    and create a Google Drive remote "
            f"named '{remote_name}'.\n"
            "\n"
            "Until then the backup still runs and still writes to the hard disk "
            "and the USB drive — only the Google Drive copy is skipped."
        )

    try:
        proc = subprocess.run(  # binary comes from settings, never from input
            [binary, "listremotes"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"Could not run rclone: {exc}"

    if proc.returncode != 0:
        return False, f"rclone would not start: {(proc.stderr or proc.stdout).strip()}"

    configured = {line.strip().rstrip(":") for line in proc.stdout.splitlines() if line.strip()}
    if remote_name not in configured:
        found = ", ".join(sorted(configured)) or "none"
        return False, (
            f"rclone is installed but has no remote called '{remote_name}'.\n"
            f"Remotes it does have: {found}.\n"
            "\n"
            f"Run:  rclone config    and create a Google Drive remote named "
            f"'{remote_name}',\n"
            "or point BACKUP_RCLONE_REMOTE in .env at one of the remotes above."
        )

    return True, ""


def push_to_drive(archive: Path) -> StepResult:
    """`rclone copy` the archive to the configured remote.

    Shelling out rather than using the Google API is the decision that keeps
    this maintainable: no OAuth flow in this codebase, no client secret in git,
    no token refresh to debug. Somebody runs ``rclone config`` once and answers
    questions in a browser.
    """
    started = time.monotonic()
    ok, message = rclone_available()
    if not ok:
        return StepResult(
            destination=Destination.DRIVE,
            outcome=Outcome.FAILED,
            target=settings.BACKUP_RCLONE_REMOTE,
            message=message,
        )

    remote = settings.BACKUP_RCLONE_REMOTE
    try:
        proc = subprocess.run(  # binary and remote come from settings
            [settings.BACKUP_RCLONE_BIN, "copy", str(archive), remote, "--no-traverse"],
            capture_output=True,
            text=True,
            timeout=settings.BACKUP_RCLONE_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return StepResult(
            destination=Destination.DRIVE,
            outcome=Outcome.FAILED,
            target=remote,
            message=(
                f"The upload to Google Drive took longer than "
                f"{settings.BACKUP_RCLONE_TIMEOUT} seconds and was stopped. "
                "The internet connection may be down or very slow. The copies on "
                "the hard disk and the USB drive were still made."
            ),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return StepResult(
            destination=Destination.DRIVE,
            outcome=Outcome.FAILED,
            target=remote,
            message=f"Could not run rclone: {exc}",
        )

    duration = int((time.monotonic() - started) * 1000)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        return StepResult(
            destination=Destination.DRIVE,
            outcome=Outcome.FAILED,
            target=remote,
            message=(
                f"rclone could not upload the backup.\n{detail}\n\n"
                "If this says the token has expired, run:  rclone config reconnect "
                f"{remote.split(':', 1)[0]}:"
            ),
            duration_ms=duration,
        )

    return StepResult(
        destination=Destination.DRIVE, outcome=Outcome.OK, target=remote, duration_ms=duration
    )


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def log_run(result: RunResult, *, user=None) -> list[BackupLog]:
    """Write one :class:`BackupLog` row per destination attempted."""
    rows = [
        BackupLog(
            run_id=result.run_id,
            destination=step.destination,
            outcome=step.outcome,
            filename=result.archive.name if result.archive else "",
            size_bytes=result.size_bytes,
            sha256=result.sha256,
            target=step.target,
            message=step.message,
            duration_ms=step.duration_ms,
            created_by=user,
            updated_by=user,
        )
        for step in result.steps
    ]
    return BackupLog.objects.bulk_create(rows)


def run_backup(*, push: bool = False, usb: bool = True, user=None) -> RunResult:
    """The whole run: snapshot, archive, USB copy, Drive push, prune, log.

    The order matters. The archive is written first and everything else copies
    it, so a failure to reach Google Drive never costs you the backup — it costs
    you the offsite copy of a backup you already have.
    """
    result = create_archive()

    if usb:
        result.steps.append(copy_to_usb(result.archive))
    if push:
        result.steps.append(push_to_drive(result.archive))

    result.pruned = [p.name for p in prune()]
    log_run(result, user=user)
    return result


def last_successful_backup() -> BackupLog | None:
    """The newest local archive that was actually written.

    Local, not Drive: this answers "is there a backup", and the copy on the hard
    disk is the one that exists whether or not there was internet last night.
    """
    return (
        BackupLog.objects.filter(destination=Destination.LOCAL, outcome=Outcome.OK)
        .order_by("-created_at")
        .first()
    )


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------
def service_is_running(host: str | None = None, port: int | None = None) -> bool:
    """Is something already serving on the configured address?

    Restoring under a running waitress would have the application holding open
    handles to a database file being replaced underneath it — on Windows the
    replace fails outright, and on POSIX it silently succeeds and the process
    keeps serving the old file until it is restarted. Neither is something to
    let a non-developer discover on their own.

    A TCP connect, because it needs no PID file to go stale and no privileges.
    """
    import os

    host = host or os.environ.get("ERP_HOST") or "127.0.0.1"
    port = int(port or os.environ.get("ERP_PORT") or 8000)
    if host in ("0.0.0.0", "::"):  # a bind address; connect to loopback instead
        host = "127.0.0.1"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def restore_archive(archive: Path, *, user=None) -> dict:
    """Put an archive back. Verified first, and never without a safety copy.

    Sequence, and every step is there because of the way this goes wrong:

    1. **Verify the SHA-256**, before anything is touched. Restoring a corrupt
       file over a working installation is the one unrecoverable mistake here.
    2. **Snapshot what is there now.** Always, even when the operator is certain.
       This is the undo for "I restored the wrong night".
    3. Replace the database, and unpack the media folder alongside it.
    4. Migrate, because the archive may predate a schema change.

    Returns the before/after row counts for the caller to print.
    """
    archive = Path(archive)
    manifest = verify_archive(archive)

    before = row_counts()

    safety = create_archive(run_id=f"pre-restore-{_stamp()}")

    live = database_path()
    live.parent.mkdir(parents=True, exist_ok=True)

    # Close Django's own handle first, or the file is replaced under an open
    # connection and the next query reads a mix of old and new pages.
    connection.close()

    with zipfile.ZipFile(archive) as zf:
        db_name = manifest.get("database_name", DB_ARCHIVE_NAME)
        incoming = live.with_suffix(".incoming")
        with zf.open(db_name) as src, incoming.open("wb") as dst:
            shutil.copyfileobj(src, dst)

        # WAL and SHM belong to the file being replaced. Left behind, SQLite
        # would try to replay them over the restored database.
        for suffix in ("-wal", "-shm"):
            stale = Path(str(live) + suffix)
            if stale.exists():
                stale.unlink()

        incoming.replace(live)

        media_root = Path(settings.MEDIA_ROOT)
        for name in zf.namelist():
            if name.startswith(MEDIA_PREFIX) and not name.endswith("/"):
                target = media_root / name[len(MEDIA_PREFIX) :]
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(name) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)

    from django.core.management import call_command

    call_command("migrate", verbosity=0, interactive=False)

    after = row_counts()
    return {
        "manifest": manifest,
        "safety_archive": safety.archive,
        "before": before,
        "after": after,
        "expected": manifest.get("row_counts", {}),
    }


__all__ = [
    "BackupError",
    "RunResult",
    "StepResult",
    "create_archive",
    "last_successful_backup",
    "prune",
    "restore_archive",
    "run_backup",
    "service_is_running",
    "verify_archive",
]
