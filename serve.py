"""
Production server for the Windows PC. **The one entry point** — see CLAUDE.md §8.

    set DJANGO_SETTINGS_MODULE=config.settings.prod
    python serve.py

Normally nobody types that: `deploy\\windows\\install.bat` registers this file
as a Windows service through NSSM, so it starts when the PC does and nobody has
to remember anything. Running it by hand in a command window is the fallback
when the service will not start and somebody needs to see the error.

waitress is a pure-Python WSGI server, so this needs no compiler, no reverse
proxy and no IIS. WhiteNoise serves static/dist from inside the same process.
There is deliberately nothing in front of it: an office PC on a LAN has no nginx
to configure and nobody to configure it.

Why the defaults are what they are
----------------------------------
``0.0.0.0`` because the whole point is that the other counter PCs reach it; a
server bound to 127.0.0.1 is a server only this machine can use, which is the
single most likely way to end up with "other PCs can't connect".

``threads=8`` because SQLite serialises writes anyway (CLAUDE.md §4 —
``transaction_mode: IMMEDIATE`` takes the write lock at BEGIN). Eight is enough
to keep a handful of counters and their PDF downloads responsive while a bill
posts, and small enough that eight threads cannot pile up on the write lock
long enough to hit the 20-second busy timeout.

Every one of the three is overridable in .env (``ERP_HOST``, ``ERP_PORT``,
``ERP_THREADS``) — but changing the port also means changing the firewall rule
install.bat added, which is why the .env comment says so.
"""

import logging
import os
import sys

import environ
from waitress import serve

HERE = os.path.dirname(os.path.abspath(__file__))

# Before anything imports Django. The service passes this in its environment as
# well; the default is here so that running `python serve.py` by hand to debug a
# failed start does not silently come up on the *development* settings and
# appear to work.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.prod")

env = environ.Env()
environ.Env.read_env(os.path.join(HERE, ".env"))

from config.wsgi import application  # noqa: E402  (must follow settings setup)

log = logging.getLogger("erp.serve")


def main():
    host = env("ERP_HOST", default="0.0.0.0")
    port = env.int("ERP_PORT", default=8000)
    threads = env.int("ERP_THREADS", default=8)

    banner = f"ERP serving on http://{host}:{port}/ with {threads} threads"

    # Both, on purpose. Under NSSM stdout goes to logs\service-out.log, which is
    # where somebody looks when the service is the problem; the logger goes to
    # logs\erp.log, which is where they look when the *application* is. A start
    # that is missing from one of them tells you which half failed.
    print(banner)
    print("Press Ctrl+C to stop.")
    log.info(banner)

    try:
        serve(application, host=host, port=port, threads=threads, ident="ERP")
    except OSError as exc:
        # Almost always "only one usage of each socket address is permitted" —
        # the ERP is already running, or something else took 8000. A traceback
        # here would be read by somebody who cannot act on it.
        message = (
            f"Could not start the ERP on {host}:{port}.\n"
            f"{exc}\n\n"
            "The usual cause is that it is already running. Check with:\n"
            "    sc query ERP\n"
            "and see TROUBLESHOOTING.md, 'The service will not start'."
        )
        log.error(message)
        print(message, file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
