"""Write C:\\erp\\.env on a fresh install. Run by install.bat, never by hand.

    python deploy\\windows\\bootstrap_env.py C:\\erp

Three things a .bat file does badly and this does well:

* **Generating a secret.** Batch has no randomness worth the name. This uses
  ``secrets``, which is the standard library's cryptographic source.
* **Finding the LAN address and subnet.** Parsing ``ipconfig`` output in batch
  means string-slicing localised text; one Windows display language and it
  breaks. A socket call gets the real answer in three lines.
* **Writing a file with '=' and ',' in it.** Batch escaping of those is a
  well-known source of silently truncated lines.

**It never overwrites an existing .env.** On a re-run or an update the file
already holds the SECRET_KEY every session cookie was signed with, and
regenerating it would sign every user out and invalidate every existing session
for no reason. If the file is there, this prints what it found and stops.

Standard library only, on purpose: install.bat calls this immediately after
creating the virtualenv, and it must work whether or not pip has run yet.
"""

from __future__ import annotations

import secrets
import socket
import string
import sys
from pathlib import Path

#: The alphabet Django's own get_random_secret_key uses. Reproduced rather than
#: imported so this runs before pip install has put Django in the virtualenv.
SECRET_ALPHABET = string.ascii_lowercase + string.digits + "!@#$%^&*(-_=+)"
SECRET_LENGTH = 50


def generate_secret_key() -> str:
    return "".join(secrets.choice(SECRET_ALPHABET) for _ in range(SECRET_LENGTH))


def lan_address() -> str:
    """This machine's LAN IPv4, or "" if it cannot be worked out.

    Connecting a UDP socket picks a route without sending a packet, which is the
    reliable way to ask "which of my addresses would a machine on the LAN see".
    ``gethostbyname(gethostname())`` is not: on Windows it frequently answers
    127.0.0.1 or a stale entry from a network the PC left months ago.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(0.5)
            sock.connect(("10.255.255.255", 1))
            return sock.getsockname()[0]
    except OSError:
        return ""


def subnet_of(address: str) -> str:
    """``192.168.1.50`` -> ``192.168.1.0/24``.

    A /24 is assumed rather than read off the adapter. Every office LAN this
    will run on is one, and the cost of being wrong is one corrected line in
    .env rather than a failed install — whereas reading the real mask means
    parsing localised ipconfig output, which is the thing this script exists to
    avoid.
    """
    parts = address.split(".")
    if len(parts) != 4:
        return ""
    return f"{parts[0]}.{parts[1]}.{parts[2]}.0/24"


TEMPLATE = """\
# Settings for this installation. Written by install.bat on {stamp}.
#
# This file is NOT in the source zip and is never overwritten by an update.
# It holds the key every login session is signed with — keep it, and do not
# copy it to another machine.

DJANGO_SETTINGS_MODULE=config.settings.prod

SECRET_KEY={secret_key}

DEBUG=False

# Names and addresses this server will answer to. The subnet line is what keeps
# it working when the router hands this PC a different address: it expands to
# every address in the office LAN. See config/settings/prod.py.
ALLOWED_HOSTS={allowed_hosts}
ALLOWED_HOSTS_SUBNET={subnet}

# Only needed if a real HTTPS certificate is ever put in front of this.
CSRF_TRUSTED_ORIGINS=
USE_TLS=False

TIME_ZONE=Asia/Karachi
LANGUAGE_CODE=en-us

# Leave False unless this office genuinely invoices before the goods receipt is
# entered. A negative stock balance has no cost behind it, so everything issued
# while it is under water is valued approximately.
ALLOW_NEGATIVE_STOCK=False

# The web server. Do not change the port without also changing the firewall
# rule that install.bat added.
ERP_HOST=0.0.0.0
ERP_PORT=8000
ERP_THREADS=8

# --- Backups -------------------------------------------------------------
# Where the USB copy goes. Set this to a folder on the memory stick, e.g.
#   BACKUP_USB_PATH=E:\\erp-backups
# An absent stick is a warning, never a failure — the backup is still taken.
BACKUP_USB_PATH=

# Google Drive, through rclone. See INSTALL-WINDOWS.md step 8.
BACKUP_RCLONE_REMOTE=gdrive:erp-backups
"""


def main(argv: list[str]) -> int:
    # install.bat asks for just the address at the end, to print the URL the
    # other PCs will use. Same detection as below, so the banner and the .env
    # can never name two different addresses.
    if "--print-lan-address" in argv:
        print(lan_address())
        return 0

    root = Path(argv[1]).resolve() if len(argv) > 1 else Path.cwd()
    env_file = root / ".env"

    if env_file.exists():
        print(f"  .env already exists at {env_file} - keeping it.")
        print("  (Its SECRET_KEY signs every login session; replacing it would")
        print("   sign everybody out. Delete the file by hand if you really mean to.)")
        return 0

    address = lan_address()
    subnet = subnet_of(address)

    hosts = ["localhost", "127.0.0.1"]
    hostname = socket.gethostname()
    if hostname and hostname not in hosts:
        hosts.append(hostname)
    if address and address not in hosts:
        hosts.append(address)

    import datetime as dt

    env_file.write_text(
        TEMPLATE.format(
            stamp=dt.datetime.now().strftime("%d %b %Y at %H:%M"),
            secret_key=generate_secret_key(),
            allowed_hosts=",".join(hosts),
            subnet=subnet,
        ),
        encoding="utf-8",
    )

    print(f"  Wrote {env_file}")
    print(f"  This PC is {hostname} at {address or 'an address that could not be detected'}")
    if subnet:
        print(f"  The whole {subnet} subnet is allowed, so a changed IP will not break it.")
    else:
        print("  WARNING: no LAN address detected. Other PCs may not be able to connect.")
        print("           Run 'ipconfig', then add the IPv4 Address to ALLOWED_HOSTS in .env.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
