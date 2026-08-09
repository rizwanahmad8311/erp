# Development helpers. Everything here runs against the Docker dev container on
# macOS. None of it exists on the Windows production machine — see CLAUDE.md.

DC := docker compose -f docker/compose.yaml
RUN := $(DC) exec web
# Use for targets that must work before `make up`.
RUN_ONESHOT := $(DC) run --rm web

TAILWIND_BIN := bin/tailwindcss
TAILWIND_MAC := bin/tailwindcss-macos-arm64
TAILWIND_URL := https://github.com/tailwindlabs/tailwindcss/releases/latest/download

# --- the Windows release ---------------------------------------------------
# What the office PC gets: one zip, no internet needed at the far end.
#
# The target Python is pinned because the wheels are downloaded FOR it. pip
# picks a wheel by (platform, python version, ABI), so cp312 wheels install on
# 3.12 and are refused by 3.11 and 3.13 — which is why install.bat checks the
# version before it does anything, and why this and that check must agree.
WIN_PY_VERSION  := 3.12.10
WIN_PY_TAG      := 312
WIN_PLATFORM    := win_amd64
WIN_PY_URL      := https://www.python.org/ftp/python/$(WIN_PY_VERSION)/python-$(WIN_PY_VERSION)-amd64.exe
NSSM_VERSION    := 2.24
NSSM_URL        := https://nssm.cc/release/nssm-$(NSSM_VERSION).zip
RCLONE_URL      := https://downloads.rclone.org/rclone-current-windows-amd64.zip

RELEASE_DIR   := deploy/windows
RELEASE_STAGE := dist/stage
RELEASE_OUT   := dist

.DEFAULT_GOAL := help
.PHONY: help up down build logs shell dbshell test lint fmt migrate makemigrations \
        superuser css css-watch js tailwind check collectstatic clean bash \
        build-release release-clean release-wheels release-vendor release-python

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

# ===========================================================================
# The Windows release
# ===========================================================================
# `make build-release` on the Mac produces dist/erp-release-<version>.zip: the
# one file that goes on a USB stick to the office.
#
# The whole point is that the far end needs NOTHING but this zip. So everything
# it could possibly want is fetched here, where there is internet, and nothing
# is fetched there, where there is not:
#
#   source          the application, minus tests, dev tooling and any data
#   static/dist     the compiled CSS/JS, already built (no node on Windows)
#   wheels/         every dependency as a cp312/win_amd64 wheel
#   vendor/nssm.exe the shim that makes a Python process a Windows service
#   vendor/rclone.exe  the Google Drive uploader
#   python/         the Python 3.12 installer itself
#   deploy/windows/ the .bat files and the two documents
#
# The three downloads are cached: once fetched they are reused, so a rebuild is
# fast and works offline. Delete deploy/windows/vendor to force a refetch.

release-clean:  ## Delete the staged release (keeps the cached downloads)
	rm -rf $(RELEASE_STAGE)

release-python:  ## Download the Python 3.12 Windows installer (cached)
	@mkdir -p $(RELEASE_DIR)/python
	@if [ -f "$(RELEASE_DIR)/python/python-$(WIN_PY_VERSION)-amd64.exe" ]; then \
	  echo "  python-$(WIN_PY_VERSION)-amd64.exe already downloaded"; \
	else \
	  echo "  downloading Python $(WIN_PY_VERSION) for Windows..."; \
	  curl -fSL --retry 3 -o "$(RELEASE_DIR)/python/python-$(WIN_PY_VERSION)-amd64.exe" "$(WIN_PY_URL)" \
	    || { echo "FAILED to download Python. Check the URL in the Makefile:"; echo "  $(WIN_PY_URL)"; exit 1; }; \
	fi

release-vendor:  ## Download nssm.exe and rclone.exe for Windows (cached)
	@mkdir -p $(RELEASE_DIR)/vendor
	@if [ -f "$(RELEASE_DIR)/vendor/nssm.exe" ]; then \
	  echo "  nssm.exe already downloaded"; \
	else \
	  echo "  downloading NSSM $(NSSM_VERSION)..."; \
	  tmp=$$(mktemp -d) && \
	  curl -fSL --retry 3 -o "$$tmp/nssm.zip" "$(NSSM_URL)" && \
	  unzip -q -o "$$tmp/nssm.zip" -d "$$tmp" && \
	  cp "$$tmp/nssm-$(NSSM_VERSION)/win64/nssm.exe" "$(RELEASE_DIR)/vendor/nssm.exe" && \
	  rm -rf "$$tmp" && echo "  got nssm.exe (win64)"; \
	fi
	@if [ -f "$(RELEASE_DIR)/vendor/rclone.exe" ]; then \
	  echo "  rclone.exe already downloaded"; \
	else \
	  echo "  downloading rclone for Windows..."; \
	  tmp=$$(mktemp -d) && \
	  curl -fSL --retry 3 -o "$$tmp/rclone.zip" "$(RCLONE_URL)" && \
	  unzip -q -o "$$tmp/rclone.zip" -d "$$tmp" && \
	  find "$$tmp" -name rclone.exe -exec cp {} "$(RELEASE_DIR)/vendor/rclone.exe" \; && \
	  rm -rf "$$tmp" && echo "  got rclone.exe"; \
	fi
	@test -f "$(RELEASE_DIR)/vendor/nssm.exe" || { echo "FAILED: no nssm.exe"; exit 1; }

release-wheels:  ## Download every dependency as a Windows cp312 wheel
	@rm -rf $(RELEASE_DIR)/wheels
	@mkdir -p $(RELEASE_DIR)/wheels
	@echo "  downloading wheels for $(WIN_PLATFORM) / cp$(WIN_PY_TAG)..."
	@# --only-binary=:all: is the load-bearing flag. Without it pip may hand back
	@# a source tarball, which needs a compiler at the far end -- and there is no
	@# compiler on the office PC (CLAUDE.md section 8). Failing here is correct:
	@# it means a dependency was added that cannot be installed on Windows
	@# offline, and that has to be fixed now rather than discovered on site.
	$(RUN_ONESHOT) pip download \
	  --dest /app/$(RELEASE_DIR)/wheels \
	  --requirement /app/requirements.txt \
	  --platform $(WIN_PLATFORM) \
	  --python-version $(WIN_PY_TAG) \
	  --only-binary=:all: \
	  --no-cache-dir
	@# pip itself, so install.bat can upgrade it offline before installing.
	$(RUN_ONESHOT) pip download \
	  --dest /app/$(RELEASE_DIR)/wheels \
	  --platform $(WIN_PLATFORM) --python-version $(WIN_PY_TAG) \
	  --only-binary=:all: --no-cache-dir pip setuptools wheel || true
	@echo "  $$(ls $(RELEASE_DIR)/wheels | wc -l | tr -d ' ') wheels"

build-release: css release-python release-vendor release-wheels  ## Build dist/erp-release-<version>.zip for the office PC
	@set -e; \
	VERSION=$$(grep -E '^APP_VERSION' config/settings/base.py | head -1 | cut -d'"' -f2); \
	NAME="erp-release-$$VERSION"; \
	echo ""; \
	echo "Building $$NAME ..."; \
	rm -rf $(RELEASE_STAGE); \
	mkdir -p $(RELEASE_STAGE)/$$NAME; \
	\
	echo "  copying the application..."; \
	for item in apps config templates static manage.py serve.py requirements.txt CLAUDE.md .env.example; do \
	  cp -R "$$item" "$(RELEASE_STAGE)/$$NAME/"; \
	done; \
	mkdir -p "$(RELEASE_STAGE)/$$NAME/deploy"; \
	cp deploy/README.md "$(RELEASE_STAGE)/$$NAME/deploy/"; \
	cp -R $(RELEASE_DIR) "$(RELEASE_STAGE)/$$NAME/deploy/windows"; \
	\
	echo "  removing what must not ship..."; \
	find "$(RELEASE_STAGE)/$$NAME" -name '__pycache__' -type d -prune -exec rm -rf {} + ; \
	find "$(RELEASE_STAGE)/$$NAME" -name '*.py[co]' -delete; \
	find "$(RELEASE_STAGE)/$$NAME" -name '.DS_Store' -delete; \
	rm -rf "$(RELEASE_STAGE)/$$NAME/static/src"; \
	@# Development-only, and dangerous on a real installation: seed_volume
	@# bulk-writes fake rows straight into the append-only ledger (CLAUDE.md
	@# §3), stepping around the guard on purpose because it is a profiling
	@# fixture. It has no business being one typo away from somebody's books,
	@# so it does not ship. config/settings/profile.py goes with it.
	rm -f "$(RELEASE_STAGE)/$$NAME/apps/core/management/commands/seed_volume.py"; \
	rm -f "$(RELEASE_STAGE)/$$NAME/config/settings/profile.py"; \
	rm -f "$(RELEASE_STAGE)/$$NAME/config/settings/test.py"; \
	\
	printf '%s\n' "$$VERSION" > "$(RELEASE_STAGE)/$$NAME/deploy/windows/VERSION.txt"; \
	printf 'Built %s from %s\n' "$$(date '+%Y-%m-%d %H:%M')" "$$(git rev-parse --short HEAD 2>/dev/null || echo 'no git')" \
	  >> "$(RELEASE_STAGE)/$$NAME/deploy/windows/VERSION.txt"; \
	\
	echo "  checking the release is complete..."; \
	test -f "$(RELEASE_STAGE)/$$NAME/manage.py"                          || { echo "MISSING manage.py"; exit 1; }; \
	test -f "$(RELEASE_STAGE)/$$NAME/serve.py"                           || { echo "MISSING serve.py"; exit 1; }; \
	test -f "$(RELEASE_STAGE)/$$NAME/static/dist/app.css"                || { echo "MISSING compiled CSS - run make css"; exit 1; }; \
	test -f "$(RELEASE_STAGE)/$$NAME/deploy/windows/install.bat"         || { echo "MISSING install.bat"; exit 1; }; \
	test -f "$(RELEASE_STAGE)/$$NAME/deploy/windows/INSTALL-WINDOWS.md"  || { echo "MISSING the install guide"; exit 1; }; \
	test -f "$(RELEASE_STAGE)/$$NAME/deploy/windows/TROUBLESHOOTING.md"  || { echo "MISSING the troubleshooting guide"; exit 1; }; \
	test -f "$(RELEASE_STAGE)/$$NAME/deploy/windows/VERIFICATION-CHECKLIST.md" || { echo "MISSING the verification checklist"; exit 1; }; \
	test -f "$(RELEASE_STAGE)/$$NAME/deploy/windows/uninstall.bat"       || { echo "MISSING uninstall.bat"; exit 1; }; \
	test -f "$(RELEASE_STAGE)/$$NAME/deploy/windows/update.bat"          || { echo "MISSING update.bat"; exit 1; }; \
	test -f "$(RELEASE_STAGE)/$$NAME/deploy/windows/erp-backup-nightly.xml" || { echo "MISSING the backup task"; exit 1; }; \
	test -f "$(RELEASE_STAGE)/$$NAME/deploy/windows/vendor/nssm.exe"     || { echo "MISSING nssm.exe"; exit 1; }; \
	test -d "$(RELEASE_STAGE)/$$NAME/deploy/windows/wheels"              || { echo "MISSING the wheels"; exit 1; }; \
	ls "$(RELEASE_STAGE)/$$NAME/deploy/windows/python/"*.exe >/dev/null   || { echo "MISSING the Python installer"; exit 1; }; \
	test ! -e "$(RELEASE_STAGE)/$$NAME/.env"                             || { echo "REFUSING: a .env got in - it holds this machine's secret key"; exit 1; }; \
	test ! -e "$(RELEASE_STAGE)/$$NAME/data"                             || { echo "REFUSING: the data folder got in - that is somebody's accounts"; exit 1; }; \
	test ! -e "$(RELEASE_STAGE)/$$NAME/media"                            || { echo "REFUSING: the media folder got in"; exit 1; }; \
	\
	echo "  zipping..."; \
	mkdir -p $(RELEASE_OUT); \
	rm -f "$(RELEASE_OUT)/$$NAME.zip"; \
	(cd $(RELEASE_STAGE) && zip -qr "../../$(RELEASE_OUT)/$$NAME.zip" "$$NAME"); \
	rm -rf $(RELEASE_STAGE); \
	echo ""; \
	echo "==========================================================="; \
	echo "  $(RELEASE_OUT)/$$NAME.zip"; \
	echo "  $$(du -h "$(RELEASE_OUT)/$$NAME.zip" | cut -f1)"; \
	echo "==========================================================="; \
	echo ""; \
	echo "  Copy it to a USB stick. On the office PC, follow"; \
	echo "  deploy/windows/INSTALL-WINDOWS.md from step 1."; \
	echo ""
