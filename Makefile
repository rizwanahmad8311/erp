# Development helpers. Everything here runs against the Docker dev container on
# macOS. None of it exists on the Windows production machine — see CLAUDE.md.

DC := docker compose -f docker/compose.yaml
RUN := $(DC) exec web
# Use for targets that must work before `make up`.
RUN_ONESHOT := $(DC) run --rm web

TAILWIND_BIN := bin/tailwindcss
TAILWIND_MAC := bin/tailwindcss-macos-arm64
TAILWIND_URL := https://github.com/tailwindlabs/tailwindcss/releases/latest/download

.DEFAULT_GOAL := help
.PHONY: help up down build logs shell dbshell test lint fmt migrate makemigrations \
        superuser css css-watch js tailwind check collectstatic clean bash

help:  ## List available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

up:  ## Build if needed and start the dev server on http://localhost:8000
	$(DC) up -d --build
	@echo "ERP running at http://localhost:8000/admin/"

down:  ## Stop the container (the SQLite volume is preserved)
	$(DC) down

build:  ## Rebuild the image without starting it
	$(DC) build

logs:  ## Tail the dev server log
	$(DC) logs -f web

shell:  ## Django shell inside the container
	$(RUN) python manage.py shell

bash:  ## Plain bash shell inside the container
	$(RUN) bash

dbshell:  ## sqlite3 prompt against the dev database
	$(RUN) python manage.py dbshell

test:  ## Run the pytest suite
	$(RUN) pytest

lint:  ## ruff check + format check
	$(RUN) ruff check .
	$(RUN) ruff format --check .

fmt:  ## Apply ruff formatting and autofixes
	$(RUN) ruff check --fix .
	$(RUN) ruff format .

migrate:  ## Apply migrations
	$(RUN) python manage.py migrate

makemigrations:  ## Generate migrations
	$(RUN) python manage.py makemigrations

superuser:  ## Create an admin user
	$(RUN) python manage.py createsuperuser

check:  ## Django system checks, production profile
	$(RUN) python manage.py check --deploy --settings=config.settings.prod

collectstatic:  ## Gather static/dist into staticfiles/ (what prod does on deploy)
	$(RUN) python manage.py collectstatic --noinput

# --- CSS ------------------------------------------------------------------
# The Tailwind standalone binary, not npm. It is fetched into bin/ inside the
# dev container and is git-ignored; production consumes the committed output in
# static/dist and never builds anything.

tailwind:  ## Fetch the Tailwind standalone CLI into the container (dev only)
	$(RUN) sh -c 'mkdir -p bin && \
	  arch=$$(uname -m); case $$arch in aarch64|arm64) t=linux-arm64 ;; *) t=linux-x64 ;; esac; \
	  curl -sSLo $(TAILWIND_BIN) $(TAILWIND_URL)/tailwindcss-$$t && chmod +x $(TAILWIND_BIN)'

css: tailwind js  ## Compile static/src -> static/dist (commit the result)
	$(RUN) ./$(TAILWIND_BIN) -i static/src/css/app.css -o static/dist/app.css --minify
	@echo "Rebuilt static/dist — commit it."

js:  ## Copy JS and fonts from static/src into static/dist (no bundler)
	$(RUN) sh -c 'mkdir -p static/dist/js static/dist/fonts && \
	  cp -f static/src/js/*.js static/dist/js/ 2>/dev/null || true; \
	  cp -f static/src/js/vendor/*.js static/dist/js/ 2>/dev/null || true; \
	  cp -f static/src/fonts/*.woff2 static/dist/fonts/ 2>/dev/null || true'

css-watch:  ## Recompile CSS on change
	$(RUN) ./$(TAILWIND_BIN) -i static/src/css/app.css -o static/dist/app.css --watch

# The same build, run natively on the Mac instead of through the container.
# Identical input and identical output — the standalone binary has no
# dependencies and no config beyond the @source lines in app.css — so which one
# you use is a matter of whether the container happens to be up.
#
# Neither exists on the Windows box. Production runs `collectstatic`, which
# copies the committed static/dist, and builds nothing.
tailwind-mac:  ## Fetch the macOS Tailwind CLI onto the host (dev only)
	@mkdir -p bin
	curl -sSLo $(TAILWIND_MAC) $(TAILWIND_URL)/tailwindcss-macos-arm64
	@chmod +x $(TAILWIND_MAC)

css-mac: tailwind-mac  ## Compile CSS + copy assets on the Mac, no Docker
	@mkdir -p static/dist/js static/dist/fonts
	cp -f static/src/js/*.js static/dist/js/
	cp -f static/src/js/vendor/*.js static/dist/js/
	cp -f static/src/fonts/*.woff2 static/dist/fonts/
	./$(TAILWIND_MAC) -i static/src/css/app.css -o static/dist/app.css --minify
	@echo "Rebuilt static/dist — commit it."

clean:  ## Remove caches and collectstatic output
	$(RUN) sh -c 'rm -rf staticfiles .pytest_cache .ruff_cache && \
	  find . -name __pycache__ -type d -prune -exec rm -rf {} +'
