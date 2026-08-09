"""The backup screen.

One page, behind ``backup.run_backup``: take a backup now, see the history, and
download a file to whatever machine the browser is on. That last one is the
point of putting this on a screen at all — an operator who can reach the ERP
from the counter PC can pull a copy off the server without knowing where on the
disk it lives.

Downloading is behind ``restore_backup``, not ``run_backup``. The archive is the
entire business: every price, every customer, every posting. Taking one is
routine; walking out of the building with one is not.

The run happens on a background thread and the page polls, because pushing to
Google Drive is a network call that can take a minute and a request that sits
there for a minute looks like a hung browser. The thread writes ``BackupLog``
rows exactly as the management command does — the screen and the scheduled task
run the same service function, so they cannot drift.
"""

from __future__ import annotations

import threading

from django.conf import settings
from django.core.exceptions import SuspiciousFileOperation
from django.http import FileResponse, Http404
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.access import module_required
from apps.accounts.permissions import RESTORE_BACKUP, RUN_BACKUP

from .models import BackupLog, Destination, Outcome
from .services import BackupError, last_successful_backup, log_run, rclone_available, run_backup

#: Set while a run is in flight, so a second click does not start a second
#: VACUUM against the same database. A module-level flag is the right scope:
#: production is a single waitress process (CLAUDE.md §8), and a database row or
#: a cache key would be a lock that outlives the process that took it — leaving
#: the button dead until somebody restarted the server.
_running = threading.Lock()


def _is_running() -> bool:
    return _running.locked()


def _backup_thread(*, push: bool, user_id: int | None):
    """Run the backup, then release the flag. Never raises into the thread."""
    from django.contrib.auth.models import User
    from django.db import connection

    user = User.objects.filter(pk=user_id).first() if user_id else None
    try:
        run_backup(push=push, user=user)
    except BackupError as exc:
        # The archive could not be written at all. That still has to reach the
        # history, or the screen shows the previous run and looks fine.
        from .services import RunResult, StepResult, _stamp

        log_run(
            RunResult(
                run_id=_stamp(),
                steps=[
                    StepResult(
                        destination=Destination.LOCAL,
                        outcome=Outcome.FAILED,
                        message=str(exc),
                    )
                ],
            ),
            user=user,
        )
    finally:
        # A thread holds its own database connection; leaving it open leaks a
        # file handle onto the SQLite file on every run.
        connection.close()
        if _running.locked():
            _running.release()


def _context(request) -> dict:
    last = last_successful_backup()
    age_hours = None
    if last:
        age_hours = (timezone.now() - last.created_at).total_seconds() / 3600

    rclone_ok, rclone_message = rclone_available()
    return {
        "logs": BackupLog.objects.all()[:60],
        "last_success": last,
        "age_hours": age_hours,
        # The screen paints this in the alarm colour. Rust means "something has
        # gone wrong or been undone" everywhere else in this system, and a
        # backup that is two days old is exactly that.
        "is_stale": last is None or age_hours > settings.BACKUP_STALE_HOURS,
        "stale_after_hours": settings.BACKUP_STALE_HOURS,
        "running": _is_running(),
        "rclone_ok": rclone_ok,
        "rclone_message": rclone_message,
        "usb_path": settings.BACKUP_USB_PATH,
        "remote": settings.BACKUP_RCLONE_REMOTE,
        "may_download": request.user.has_perm(RESTORE_BACKUP),
    }


@module_required(RUN_BACKUP)
@require_GET
def index(request):
    """The screen."""
    return render(request, "backup/index.html", _context(request))


@module_required(RUN_BACKUP)
@require_GET
def status(request):
    """The polled fragment: the running state and the history table.

    htmx swaps this every couple of seconds while a run is in flight and stops
    on its own once it is not — see the template.
    """
    return render(request, "backup/partials/status.html", _context(request))


@module_required(RUN_BACKUP)
@require_POST
def start(request):
    """Kick a run off on a thread and hand the page straight back."""
    if not _running.acquire(blocking=False):
        return redirect("backup:index")

    push = request.POST.get("push") == "1"
    thread = threading.Thread(
        target=_backup_thread,
        kwargs={"push": push, "user_id": request.user.pk},
        daemon=True,
        name="erp-backup",
    )
    thread.start()
    return redirect("backup:index")


@module_required(RESTORE_BACKUP)
@require_GET
def download(request, filename: str):
    """Send an archive to the browser.

    The filename is resolved against ``BACKUP_ROOT`` and checked to still be
    inside it after resolution, so ``../../data/erp.sqlite3`` cannot walk out of
    the directory. Django's own ``safe_join`` does the same check, and is used
    rather than a hand-rolled one.
    """
    from django.utils._os import safe_join

    try:
        path = safe_join(settings.BACKUP_ROOT, filename)
    except SuspiciousFileOperation as exc:
        raise Http404("No such backup.") from exc

    from pathlib import Path

    resolved = Path(path)
    if not resolved.is_file() or resolved.suffix != ".zip":
        raise Http404("No such backup.")

    return FileResponse(resolved.open("rb"), as_attachment=True, filename=resolved.name)
