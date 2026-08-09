"""``python manage.py preflight [--service]`` — is this installation actually fit to run?

``manage.py check --deploy`` is Django's version of this and it is not enough
here. It knows nothing about whether ``.env`` exists, whether the machine's own
LAN address is in ``ALLOWED_HOSTS``, whether ``collectstatic`` was ever run, or
whether the folder the backups go in can be written to. Those are the four
things that actually go wrong on an office PC, and every one of them fails
*later* — at 8am on a Monday, in front of a queue — rather than at install time.

So this runs at the end of ``install.bat`` and at the end of ``update.bat``, and
it is the first thing TROUBLESHOOTING.md asks for. It is written for somebody
who is not a developer: every line is ``OK``, ``WARN`` or ``FAIL`` followed by a
sentence, and every ``FAIL`` says what to do.

    OK    DEBUG is off.
    FAIL  This PC's address 192.168.1.50 is not in ALLOWED_HOSTS.
          Other PCs will get "Bad Request (400)". Add it to C:\\erp\\.env.

Exit codes, because a .bat file reads numbers and not prose:

* ``0`` — everything passed, or the only problems were warnings.
* ``1`` — at least one check failed. The install is not finished.

``--service`` additionally waits for the HTTP port to answer, which is the one
check that cannot be made before the service is started.
"""

from __future__ import annotations

import os
import socket
import urllib.error
import urllib.request
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection

#: How long ``--service`` waits for waitress to come up. Generous: the first
#: start on a cold office PC loads Django, opens SQLite and warms WhiteNoise's
#: manifest, and a check that gave up at three seconds would fail an install
#: that is fine.
SERVICE_TIMEOUT_SECONDS = 60

#: The placeholder in base.py. An installation still running on it has a
#: SECRET_KEY that is in the public source tree.
DEV_SECRET_KEY = "insecure-development-key-do-not-deploy"

#: Shortest key worth accepting. Django generates 50 characters.
MIN_SECRET_KEY_LENGTH = 32


class Result:
    """One line of the report, and whether it sinks the install."""

    def __init__(self, level: str, message: str, advice: str = ""):
        self.level = level
        self.message = message
        self.advice = advice

    @property
    def failed(self) -> bool:
        return self.level == "FAIL"


def ok(message: str) -> Result:
    return Result("OK", message)


def warn(message: str, advice: str = "") -> Result:
    return Result("WARN", message, advice)


def fail(message: str, advice: str = "") -> Result:
    return Result("FAIL", message, advice)


def lan_addresses() -> list[str]:
    """Every IPv4 address this machine answers on, best effort.

    The UDP-connect trick rather than ``gethostbyname(gethostname())``: on
    Windows the hostname often resolves to 127.0.0.1 or to a stale entry, and
    what this check is about is the address the counter PCs will type. No packet
    is sent — connecting a UDP socket only picks the route.
    """
    found: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(0.5)
            sock.connect(("10.255.255.255", 1))
            found.append(sock.getsockname()[0])
    except OSError:
        pass

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = info[4][0]
            if address not in found and not address.startswith("127."):
                found.append(address)
    except OSError:
        pass
    return found


class Command(BaseCommand):
    help = "Check that this installation is configured to run in production."

    def add_arguments(self, parser):
        parser.add_argument(
            "--service",
            action="store_true",
            help="Also wait for the HTTP port to answer. Used after starting the service.",
        )
        parser.add_argument(
            "--timeout",
            type=int,
            default=SERVICE_TIMEOUT_SECONDS,
            help=f"Seconds to wait for the service (default {SERVICE_TIMEOUT_SECONDS}).",
        )

    def handle(self, *args, **options):
        results: list[Result] = [
            *self._check_settings_module(),
            *self._check_debug(),
            *self._check_secret_key(),
            *self._check_allowed_hosts(),
            *self._check_static(),
            *self._check_logging(),
            *self._check_database(),
            *self._check_backups(),
        ]
        if options["service"]:
            results.extend(self._check_service(options["timeout"]))

        self._report(results)

        failures = [r for r in results if r.failed]
        if failures:
            self.stderr.write(
                self.style.ERROR(
                    f"\n{len(failures)} check(s) FAILED. This installation is not ready to use.\n"
                    "Fix the items above and run this again:\n"
                    "    .venv\\Scripts\\python.exe manage.py preflight "
                    "--settings=config.settings.prod"
                )
            )
            raise SystemExit(1)

        warnings = [r for r in results if r.level == "WARN"]
        tail = f" ({len(warnings)} warning(s) — read them)" if warnings else ""
        self.stdout.write(self.style.SUCCESS(f"\nAll checks passed{tail}."))

    # ------------------------------------------------------------------
    def _report(self, results: list[Result]) -> None:
        styles = {"OK": self.style.SUCCESS, "WARN": self.style.WARNING, "FAIL": self.style.ERROR}
        self.stdout.write("")
        for result in results:
            style = styles[result.level]
            self.stdout.write(style(f"{result.level:<5} {result.message}"))
            if result.advice:
                for line in result.advice.splitlines():
                    self.stdout.write(f"      {line}")

    # ------------------------------------------------------------------
    # The checks
    # ------------------------------------------------------------------
    def _check_settings_module(self) -> list[Result]:
        module = os.environ.get("DJANGO_SETTINGS_MODULE", "")
        if module == "config.settings.prod":
            return [ok("Running with the production settings.")]
        return [
            fail(
                f"Settings module is {module or 'not set'}, not config.settings.prod.",
                "Run this with --settings=config.settings.prod, and make sure the\n"
                "service passes DJANGO_SETTINGS_MODULE=config.settings.prod.",
            )
        ]

    def _check_debug(self) -> list[Result]:
        if not settings.DEBUG:
            return [ok("DEBUG is off.")]
        return [
            fail(
                "DEBUG is ON. Error pages would show the source code and the settings.",
                "Set DEBUG=False in C:\\erp\\.env, then restart the ERP service.",
            )
        ]

    def _check_secret_key(self) -> list[Result]:
        env_file = Path(settings.BASE_DIR) / ".env"
        results = []
        if env_file.is_file():
            results.append(ok(f"Settings file found at {env_file}."))
        else:
            results.append(
                fail(
                    f"There is no .env file at {env_file}.",
                    "install.bat creates it. If it has been deleted, copy .env.example\n"
                    "to .env and put a new SECRET_KEY in it.",
                )
            )

        key = settings.SECRET_KEY or ""
        if key == DEV_SECRET_KEY:
            results.append(
                fail(
                    "SECRET_KEY is still the development placeholder from the source tree.",
                    "Anybody with a copy of the source could forge a login session.\n"
                    "Put a new random SECRET_KEY in C:\\erp\\.env and restart.",
                )
            )
        elif len(key) < MIN_SECRET_KEY_LENGTH:
            results.append(
                fail(
                    f"SECRET_KEY is only {len(key)} characters long.",
                    f"It should be at least {MIN_SECRET_KEY_LENGTH}. Generate a new one and\n"
                    "put it in C:\\erp\\.env.",
                )
            )
        else:
            results.append(ok("SECRET_KEY is set and is not the shipped placeholder."))
        return results

    def _check_allowed_hosts(self) -> list[Result]:
        hosts = list(settings.ALLOWED_HOSTS)
        if not hosts:
            return [
                fail(
                    "ALLOWED_HOSTS is empty. Every request would be refused.",
                    "Set ALLOWED_HOSTS in C:\\erp\\.env.",
                )
            ]
        if "*" in hosts:
            return [
                warn(
                    "ALLOWED_HOSTS is '*', which accepts any Host header at all.",
                    "It works, but it switches off a real protection. Prefer\n"
                    "ALLOWED_HOSTS_SUBNET=192.168.1.0/24 in C:\\erp\\.env.",
                )
            ]

        subnet = getattr(settings, "ALLOWED_HOSTS_SUBNET", "")
        results = [
            ok(
                f"ALLOWED_HOSTS covers {len(hosts)} name(s)/address(es)"
                + (f", including the subnet {subnet}." if subnet else ".")
            )
        ]

        addresses = lan_addresses()
        if not addresses:
            results.append(
                warn(
                    "Could not work out this PC's LAN address, so it was not checked.",
                    "Run  ipconfig  and confirm the IPv4 Address is in ALLOWED_HOSTS.",
                )
            )
            return results

        missing = [address for address in addresses if address not in hosts]
        if missing:
            results.append(
                fail(
                    f"This PC answers on {', '.join(missing)}, which is not in ALLOWED_HOSTS.",
                    'Other PCs on the LAN will get "Bad Request (400)".\n'
                    f"Add it in C:\\erp\\.env, either as\n"
                    f"    ALLOWED_HOSTS=localhost,127.0.0.1,{missing[0]}\n"
                    f"or better, so a changed DHCP lease does not break it again:\n"
                    f"    ALLOWED_HOSTS_SUBNET={_subnet_of(missing[0])}",
                )
            )
        else:
            results.append(ok(f"This PC's LAN address ({', '.join(addresses)}) is allowed."))
        return results

    def _check_static(self) -> list[Result]:
        results = []
        middleware = getattr(settings, "MIDDLEWARE", [])
        if any("whitenoise" in entry.lower() for entry in middleware):
            results.append(ok("WhiteNoise is serving the static files."))
        else:
            results.append(
                fail(
                    "WhiteNoise is not in MIDDLEWARE. No CSS, JS or fonts would load.",
                    "There is no web server in front of this one, so WhiteNoise is what\n"
                    "serves them. Do not remove it from config/settings/base.py.",
                )
            )

        root = Path(settings.STATIC_ROOT)
        manifest = root / "staticfiles.json"
        if not root.is_dir():
            results.append(
                fail(
                    f"collectstatic has never been run — {root} does not exist.",
                    "Every page would load without styling. Run:\n"
                    "    .venv\\Scripts\\python.exe manage.py collectstatic --noinput "
                    "--settings=config.settings.prod",
                )
            )
        elif not manifest.is_file():
            results.append(
                fail(
                    f"{root} exists but has no staticfiles.json manifest in it.",
                    "collectstatic did not finish. Run it again:\n"
                    "    .venv\\Scripts\\python.exe manage.py collectstatic --noinput "
                    "--settings=config.settings.prod",
                )
            )
        else:
            count = sum(1 for _ in root.rglob("*") if _.is_file())
            results.append(ok(f"Static files are collected ({count} files in {root})."))
        return results

    def _check_logging(self) -> list[Result]:
        handlers = settings.LOGGING.get("handlers", {})
        rotating = [
            name
            for name, config in handlers.items()
            if "RotatingFileHandler" in str(config.get("class", ""))
        ]
        if not rotating:
            return [
                fail(
                    "Logging is not writing to a rotating file.",
                    "Nothing would be recorded for anybody to look at after a problem.",
                )
            ]

        results = [ok(f"Logging to a rotating file ({', '.join(rotating)}).")]
        log_dir = Path(getattr(settings, "LOG_DIR", Path(settings.BASE_DIR) / "logs"))
        probe = log_dir / ".preflight-write-test"
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            results.append(ok(f"The log folder is writable ({log_dir})."))
        except OSError as exc:
            results.append(
                fail(
                    f"Cannot write to the log folder {log_dir}: {exc.strerror or exc}",
                    "The account the ERP service runs as needs write access to it.\n"
                    "Right-click the folder, Properties, Security, and give the\n"
                    "service account Modify permission.",
                )
            )
        return results

    def _check_database(self) -> list[Result]:
        database = Path(connection.settings_dict["NAME"])
        results = []
        if not database.is_file():
            results.append(
                fail(
                    f"The database file does not exist at {database}.",
                    "Run:\n"
                    "    .venv\\Scripts\\python.exe manage.py migrate "
                    "--settings=config.settings.prod",
                )
            )
            return results

        size_mb = database.stat().st_size / 1024 / 1024
        results.append(ok(f"Database found at {database} ({size_mb:.1f} MB)."))

        try:
            probe = database.parent / ".preflight-write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            results.append(ok("The database folder is writable."))
        except OSError as exc:
            results.append(
                fail(
                    f"Cannot write to {database.parent}: {exc.strerror or exc}",
                    "SQLite needs to write its -wal and -shm files next to the database.\n"
                    "Nobody would be able to save anything. Fix the folder permissions.",
                )
            )

        from django.db.migrations.executor import MigrationExecutor

        executor = MigrationExecutor(connection)
        pending = executor.migration_plan(executor.loader.graph.leaf_nodes())
        if pending:
            results.append(
                fail(
                    f"{len(pending)} database migration(s) have not been applied.",
                    "The application and the database disagree about the schema. Run:\n"
                    "    .venv\\Scripts\\python.exe manage.py migrate "
                    "--settings=config.settings.prod",
                )
            )
        else:
            results.append(ok("The database schema is up to date."))
        return results

    def _check_backups(self) -> list[Result]:
        root = Path(settings.BACKUP_ROOT)
        probe = root / ".preflight-write-test"
        try:
            root.mkdir(parents=True, exist_ok=True)
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            return [
                fail(
                    f"Cannot write to the backup folder {root}: {exc.strerror or exc}",
                    "The nightly backup would fail every night. Fix the permissions.",
                )
            ]

        results = [ok(f"The backup folder is writable ({root}).")]
        archives = sorted(root.glob("erp-*.zip"))
        if archives:
            results.append(ok(f"{len(archives)} backup(s) on disk, newest {archives[-1].name}."))
        else:
            results.append(
                warn(
                    "There are no backups yet.",
                    "Expected on a fresh install. Take one now to prove it works:\n"
                    "    .venv\\Scripts\\python.exe manage.py backup "
                    "--settings=config.settings.prod",
                )
            )
        return results

    def _check_service(self, timeout: int) -> list[Result]:
        """Wait for waitress to answer. The one check that needs it running."""
        import time

        host = os.environ.get("ERP_HOST", "127.0.0.1")
        if host in ("0.0.0.0", "::", ""):  # a bind address; connect to loopback
            host = "127.0.0.1"
        port = int(os.environ.get("ERP_PORT", "8000"))
        url = f"http://{host}:{port}/"

        deadline = time.monotonic() + timeout
        last_error = ""
        while time.monotonic() < deadline:
            try:
                # Any HTTP answer means waitress is up. The site redirects to the
                # login page, so a 302 is the expected success here, not a 200.
                # The URL is built from ERP_HOST/ERP_PORT and always points at
                # this machine's own loopback — never at anything a user typed.
                with urllib.request.urlopen(url, timeout=5) as response:
                    status = response.status
                return self._service_result(url, status)
            except urllib.error.HTTPError as exc:
                return self._service_result(url, exc.code)
            except (urllib.error.URLError, OSError) as exc:
                last_error = str(getattr(exc, "reason", exc))
                time.sleep(2)

        return [
            fail(
                f"Nothing answered on {url} within {timeout} seconds.",
                f"Last error: {last_error}\n"
                "The ERP service is not running. Check:\n"
                "    sc query ERP\n"
                "and look in C:\\erp\\logs\\service-err.log for the reason.\n"
                "See TROUBLESHOOTING.md, 'The service will not start'.",
            )
        ]

    def _service_result(self, url: str, status: int) -> list[Result]:
        if status >= 500:
            return [
                fail(
                    f"{url} answered with HTTP {status} — the application is erroring.",
                    "Look at the end of C:\\erp\\logs\\erp.log for the reason.",
                )
            ]
        return [ok(f"The ERP is answering on {url} (HTTP {status}).")]


def _subnet_of(address: str) -> str:
    """``192.168.1.50`` -> ``192.168.1.0/24``, for the advice text.

    A /24 guess rather than reading the adapter's real mask: this string goes
    into a sentence telling somebody what to type, and every office LAN this
    will ever run on is a /24. Being wrong here costs a corrected line in .env,
    not a broken install.
    """
    parts = address.split(".")
    if len(parts) == 4:
        return f"{parts[0]}.{parts[1]}.{parts[2]}.0/24"
    return "192.168.1.0/24"
