"""The Windows deployment: the settings check, the LAN subnet, and the release.

None of this can be run on Windows from here, so these tests cover the half that
is real code and can be: the production settings module, the ``preflight``
command's verdicts, and the helpers ``install.bat`` leans on. The Windows-only
half — the .bat files, NSSM, the firewall, Task Scheduler, printing — is covered
by ``deploy/windows/VERIFICATION-CHECKLIST.md``, which the first installer
completes by hand.

The tests that matter most here are the ones that assert ``preflight``
**fails**. A check that cannot fail is a check that passes on a broken
installation, and the whole point of that command is to be the thing that says
"no" at install time rather than at 8am on a Monday.
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from django.conf import settings
from django.core.management import call_command
from django.test import override_settings

BASE = Path(settings.BASE_DIR)
WINDOWS = BASE / "deploy" / "windows"


def load_bootstrap_env():
    """Import deploy/windows/bootstrap_env.py, which is not on the path.

    It is deliberately not a package: install.bat runs it as a plain script with
    the system Python before the virtualenv has anything in it.
    """
    spec = importlib.util.spec_from_file_location("bootstrap_env", WINDOWS / "bootstrap_env.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ===========================================================================
# The release, as a set of files
# ===========================================================================
class TestTheReleaseIsComplete:
    """Everything the office PC is promised is actually in the source tree."""

    @pytest.mark.parametrize(
        "name",
        [
            "INSTALL-WINDOWS.md",
            "TROUBLESHOOTING.md",
            "VERIFICATION-CHECKLIST.md",
            "install.bat",
            "update.bat",
            "uninstall.bat",
            "bootstrap_env.py",
            "erp-backup-nightly.xml",
        ],
    )
    def test_file_is_there(self, name):
        assert (WINDOWS / name).is_file(), f"deploy/windows/{name} is missing"

    def test_serve_py_is_at_the_root(self):
        """One entry point, named by CLAUDE.md section 8.

        Not two. A second copy under deploy/windows would be the one somebody
        edits while the service keeps running the other.
        """
        assert (BASE / "serve.py").is_file()
        assert not (WINDOWS / "serve.py").exists(), (
            "There must be exactly one serve.py, at the repository root — "
            "install.bat registers that path as the service."
        )

    def test_the_install_guide_covers_every_step_the_brief_asked_for(self):
        text = (WINDOWS / "INSTALL-WINDOWS.md").read_text(encoding="utf-8")
        for phrase in (
            "Add python.exe to PATH",  # the most-missed step
            "C:\\erp",
            "install.bat",
            "http://localhost:8000",
            "ipconfig",
            "netsh advfirewall firewall add rule",
            "rclone",
            "Import Task",
            "preflight",
        ):
            assert phrase in text, f"INSTALL-WINDOWS.md never mentions {phrase!r}"

    def test_the_troubleshooting_guide_covers_the_named_failures(self):
        text = (WINDOWS / "TROUBLESHOOTING.md").read_text(encoding="utf-8").lower()
        for phrase in (
            "the service will not start",
            "other pcs cannot connect",
            "the printer does not appear",
            "database is locked",
            "the backup fails",
            "undoing an update",
        ):
            assert phrase in text, f"TROUBLESHOOTING.md has no section for {phrase!r}"

    def test_the_scheduled_task_points_at_the_backup_command(self):
        xml = (WINDOWS / "erp-backup-nightly.xml").read_text(encoding="utf-8")
        assert "manage.py backup" in xml
        assert "--settings=config.settings.prod" in xml
        # The paths are absolute because Task Scheduler runs with no useful
        # working directory. If that ever becomes relative it fails at 21:00,
        # unattended, and the only symptom is a backup that stops happening.
        assert "C:\\erp\\.venv\\Scripts\\python.exe" in xml

    def test_install_bat_calls_what_it_says_it_calls(self):
        """The .bat cannot be executed here, so check its references resolve."""
        text = (WINDOWS / "install.bat").read_text(encoding="utf-8")
        assert "bootstrap_env.py" in text
        assert "manage.py migrate" in text
        assert "manage.py collectstatic" in text
        assert "manage.py createsuperuser" in text
        assert "manage.py preflight" in text
        assert "--no-index" in text, "the install must not reach for PyPI"
        assert "netsh advfirewall firewall add rule" in text
        assert "SERVICE_AUTO_START" in text

    def test_uninstall_bat_leaves_the_data_alone(self):
        """The one thing it must never do.

        Asserted against the text because there is no way to run it here — and
        a `rmdir` or `del` against the data folder appearing in this file is a
        change somebody has to make on purpose and defend.
        """
        text = (WINDOWS / "uninstall.bat").read_text(encoding="utf-8").lower()
        for destructive in ("rmdir /s", "del /q", "rd /s", "format "):
            assert destructive not in text, (
                f"uninstall.bat contains {destructive!r}. It removes the service, "
                f"the firewall rule and the backup task — never any data."
            )

    def test_every_requirement_can_be_named_in_one_place(self):
        """requirements.txt is what the wheels are downloaded from.

        A dependency added to INSTALLED_APPS and not to requirements.txt is one
        that is missing from the release zip and fails at pip time on site.
        """
        text = (BASE / "requirements.txt").read_text(encoding="utf-8")
        named = {
            line.split(">=")[0].split("==")[0].strip().lower().replace("-", "_")
            for line in text.splitlines()
            if line.strip() and not line.startswith("#")
        }
        for package in ("django", "django_unfold", "whitenoise", "waitress", "reportlab"):
            assert package in named, f"{package} is not in requirements.txt"


# ===========================================================================
# The LAN subnet
# ===========================================================================
class TestAllowedHostsSubnet:
    """Django understands no CIDR, so prod.py expands one. See config/settings/prod.py."""

    def _prod_settings(self, **env):
        """Import config.settings.prod in a subprocess with these env vars.

        A subprocess because the expansion happens at import time and the module
        is already imported in this process under the test settings.
        """
        environment = {
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "config.settings.prod",
            "SECRET_KEY": "x" * 50,
            "ALLOWED_HOSTS": "localhost,127.0.0.1",
            **env,
        }
        script = (
            "import django;django.setup();"
            "from django.conf import settings;"
            "print(len(settings.ALLOWED_HOSTS));"
            "print('192.168.1.77' in settings.ALLOWED_HOSTS)"
        )
        return subprocess.run(
            [sys.executable, "-c", script],
            cwd=BASE,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_a_subnet_expands_to_its_hosts(self):
        """192.168.1.0/24 -> the 254 usable addresses, plus what was listed."""
        result = self._prod_settings(ALLOWED_HOSTS_SUBNET="192.168.1.0/24")
        assert result.returncode == 0, result.stderr
        count, contains = result.stdout.split()
        assert int(count) == 256  # 2 named + 254 usable
        assert contains == "True"

    def test_no_subnet_leaves_allowed_hosts_alone(self):
        result = self._prod_settings(ALLOWED_HOSTS_SUBNET="")
        assert result.returncode == 0, result.stderr
        assert int(result.stdout.split()[0]) == 2

    def test_a_subnet_that_is_too_big_is_refused(self):
        """A typed /8 would be sixteen million strings scanned on every request."""
        result = self._prod_settings(ALLOWED_HOSTS_SUBNET="10.0.0.0/8")
        assert result.returncode != 0
        assert "more than this is meant for" in result.stderr

    def test_a_subnet_that_is_not_a_subnet_is_refused(self):
        result = self._prod_settings(ALLOWED_HOSTS_SUBNET="the office")
        assert result.returncode != 0
        assert "is not a subnet" in result.stderr

    def test_empty_allowed_hosts_is_refused(self):
        """Rather than starting a server that answers nothing."""
        result = self._prod_settings(ALLOWED_HOSTS="", ALLOWED_HOSTS_SUBNET="")
        assert result.returncode != 0
        assert "ALLOWED_HOSTS is empty" in result.stderr


# ===========================================================================
# bootstrap_env.py
# ===========================================================================
class TestBootstrapEnv:
    def test_it_generates_a_key_of_the_right_shape(self):
        module = load_bootstrap_env()
        first = module.generate_secret_key()
        second = module.generate_secret_key()
        assert len(first) == 50
        assert first != second

    @pytest.mark.parametrize(
        ("address", "expected"),
        [
            ("192.168.1.50", "192.168.1.0/24"),
            ("10.0.0.7", "10.0.0.0/24"),
            ("", ""),
            ("not-an-address", ""),
        ],
    )
    def test_subnet_of(self, address, expected):
        assert load_bootstrap_env().subnet_of(address) == expected

    def test_it_writes_a_usable_env_file(self, tmp_path):
        module = load_bootstrap_env()
        assert module.main(["bootstrap_env.py", str(tmp_path)]) == 0

        text = (tmp_path / ".env").read_text(encoding="utf-8")
        assert "DEBUG=False" in text
        assert "DJANGO_SETTINGS_MODULE=config.settings.prod" in text
        assert re.search(r"^SECRET_KEY=.{50}$", text, re.MULTILINE)
        assert "ALLOWED_HOSTS=" in text
        assert "ERP_PORT=8000" in text

    def test_it_never_overwrites_an_existing_env(self, tmp_path):
        """The SECRET_KEY in it signs every session. Replacing it signs everybody out.

        This is what makes install.bat safe to run a second time.
        """
        existing = tmp_path / ".env"
        existing.write_text("SECRET_KEY=do-not-touch-me\n", encoding="utf-8")

        module = load_bootstrap_env()
        assert module.main(["bootstrap_env.py", str(tmp_path)]) == 0
        assert existing.read_text(encoding="utf-8") == "SECRET_KEY=do-not-touch-me\n"


# ===========================================================================
# preflight
# ===========================================================================
@pytest.mark.django_db
class TestPreflight:
    """What the installer runs to find out whether the install is finished.

    Every test here that asserts a FAILURE is the important one: a check that
    cannot fail passes on a broken machine.
    """

    def _run(self, **overrides):
        """Run preflight, returning (exit_code, output)."""
        from io import StringIO

        out, err = StringIO(), StringIO()
        environment = {"DJANGO_SETTINGS_MODULE": "config.settings.prod"}
        previous = os.environ.get("DJANGO_SETTINGS_MODULE")
        os.environ.update(environment)
        try:
            with override_settings(**overrides):
                call_command("preflight", stdout=out, stderr=err)
            code = 0
        except SystemExit as exc:
            code = exc.code
        finally:
            if previous is None:
                os.environ.pop("DJANGO_SETTINGS_MODULE", None)
            else:
                os.environ["DJANGO_SETTINGS_MODULE"] = previous
        return code, out.getvalue() + err.getvalue()

    def test_debug_on_is_a_failure(self):
        code, output = self._run(DEBUG=True)
        assert code == 1
        assert "DEBUG is ON" in output

    def test_the_shipped_placeholder_key_is_a_failure(self):
        from apps.core.management.commands.preflight import DEV_SECRET_KEY

        code, output = self._run(SECRET_KEY=DEV_SECRET_KEY)
        assert code == 1
        assert "development placeholder" in output

    def test_a_short_key_is_a_failure(self):
        code, output = self._run(SECRET_KEY="tooshort")
        assert code == 1
        assert "characters long" in output

    def test_empty_allowed_hosts_is_a_failure(self):
        code, output = self._run(ALLOWED_HOSTS=[])
        assert code == 1
        assert "ALLOWED_HOSTS is empty" in output

    def test_a_wildcard_host_is_a_warning_not_a_failure(self):
        """It works. It is just weaker than naming the subnet, and it says so."""
        _code, output = self._run(ALLOWED_HOSTS=["*"])
        assert "WARN" in output
        assert "accepts any Host header" in output

    def test_removing_whitenoise_is_a_failure(self):
        """There is no web server in front of this one."""
        without = [m for m in settings.MIDDLEWARE if "whitenoise" not in m.lower()]
        code, output = self._run(MIDDLEWARE=without)
        assert code == 1
        assert "WhiteNoise is not in MIDDLEWARE" in output

    def test_missing_collectstatic_is_a_failure(self, tmp_path):
        code, output = self._run(STATIC_ROOT=str(tmp_path / "never-collected"))
        assert code == 1
        assert "collectstatic has never been run" in output

    def test_a_static_root_with_no_manifest_is_a_failure(self, tmp_path):
        (tmp_path / "collected").mkdir()
        code, output = self._run(STATIC_ROOT=str(tmp_path / "collected"))
        assert code == 1
        assert "no staticfiles.json manifest" in output

    def test_logging_without_a_rotating_file_is_a_failure(self):
        """An uncapped log on an unattended PC is a disk that fills."""
        code, output = self._run(
            LOGGING={
                "version": 1,
                "handlers": {"console": {"class": "logging.StreamHandler"}},
                "root": {"handlers": ["console"]},
            }
        )
        assert code == 1
        assert "not writing to a rotating file" in output

    def test_a_missing_env_file_is_a_failure(self, tmp_path):
        code, output = self._run(BASE_DIR=tmp_path)
        assert code == 1
        assert "no .env file" in output

    def test_it_names_the_fix_for_every_failure(self):
        """A FAIL with no advice is a dead end for somebody who is not a developer."""
        _code, output = self._run(DEBUG=True, SECRET_KEY="short")
        for marker in ("C:\\erp\\.env", "restart"):
            assert marker in output

    def test_the_subnet_advice_is_a_subnet(self):
        from apps.core.management.commands.preflight import _subnet_of

        assert _subnet_of("192.168.1.50") == "192.168.1.0/24"
        assert _subnet_of("garbage") == "192.168.1.0/24"


class TestTheReleaseLeavesOutWhatMustNotShip:
    """Some code is safe on a laptop and dangerous on somebody's books.

    ``seed_volume`` writes tens of thousands of fabricated rows straight into
    the ledger with the base manager, stepping around the append-only guard on
    purpose because it is a profiling fixture (CLAUDE.md §3). Those rows cannot
    be deleted afterwards — that is what append-only means.

    It carries its own allow-list guard as well. This is the second lock: on the
    office PC the command should not exist at all.
    """

    #: Path in the repo -> why it must not reach the Windows machine.
    MUST_NOT_SHIP = {
        "apps/core/management/commands/seed_volume.py": (
            "bulk-writes fake rows into the append-only ledger"
        ),
        "config/settings/profile.py": "points the database at a scratch file",
        "config/settings/test.py": "installs the pytest-only test app",
    }

    def test_the_makefile_deletes_each_of_them(self):
        """Asserted against the Makefile rather than a built zip.

        Building a release downloads a Python installer and every wheel, which
        is not something a unit test should do. What can be checked cheaply is
        that the recipe names each file — and if somebody adds a dangerous
        command later, the entry above is what reminds them.
        """
        from pathlib import Path

        from django.conf import settings

        makefile = (Path(settings.BASE_DIR) / "Makefile").read_text(encoding="utf-8")
        stage = makefile.split("removing what must not ship", 1)
        assert len(stage) == 2, "the release recipe no longer has a removal step"

        for path, why in self.MUST_NOT_SHIP.items():
            assert path in stage[1], (
                f"{path} would ship to the office PC, and it {why}. "
                f"Add an `rm -f` for it to the release recipe."
            )

    def test_each_of_them_actually_exists_to_be_removed(self):
        """A stale entry above is a rule that silently stops protecting anything."""
        from pathlib import Path

        from django.conf import settings

        for path in self.MUST_NOT_SHIP:
            assert (Path(settings.BASE_DIR) / path).exists(), (
                f"{path} is listed as must-not-ship but no longer exists — "
                f"remove the entry so the list stays meaningful."
            )

    def test_seed_volume_refuses_outside_the_profile_settings(self):
        """The second lock, checked directly."""
        from io import StringIO

        from django.core.management import call_command
        from django.core.management.base import CommandError

        with pytest.raises(CommandError) as caught:
            call_command("seed_volume", "--invoices", "1", stdout=StringIO())

        message = str(caught.value)
        assert "Refusing to run" in message
        assert "config.settings.profile" in message, "the refusal must name the way to run it"
