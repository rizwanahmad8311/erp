"""
Production server for the Windows PC.

    set DJANGO_SETTINGS_MODULE=config.settings.prod
    python serve.py

waitress is a pure-Python WSGI server, so this needs no compiler, no service
wrapper and no reverse proxy. WhiteNoise serves static/dist from inside the
same process.
"""

import os

import environ
from waitress import serve

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.prod")

env = environ.Env()
environ.Env.read_env(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from config.wsgi import application  # noqa: E402  (must follow settings setup)


def main():
    host = env("ERP_HOST", default="0.0.0.0")
    port = env.int("ERP_PORT", default=8000)
    threads = env.int("ERP_THREADS", default=8)

    print(f"ERP serving on http://{host}:{port}/ with {threads} threads")
    print("Press Ctrl+C to stop.")
    serve(application, host=host, port=port, threads=threads, ident="ERP")


if __name__ == "__main__":
    main()
