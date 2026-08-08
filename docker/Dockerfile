# Development image only. Production is a bare Windows PC — no Docker there.
# Keep this file free of anything production depends on.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DJANGO_SETTINGS_MODULE=config.settings.dev

WORKDIR /app

# curl is here only to fetch the Tailwind standalone binary for `make css`.
# It is a dev convenience; production ships the compiled output from git.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# Dependency layer, cached independently of the source bind mount.
COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements-dev.txt

# Source is bind-mounted at runtime (see compose.yaml); nothing is COPYed in,
# so an edit on the Mac is live in the container immediately.

EXPOSE 8000

CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
