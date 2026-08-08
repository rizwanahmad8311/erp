"""
WSGI entrypoint.

Used by waitress in production (see serve.py) and by runserver in development.
DJANGO_SETTINGS_MODULE defaults to prod because the Windows box is the only
place this file is loaded without an explicit environment.
"""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.prod")

application = get_wsgi_application()
