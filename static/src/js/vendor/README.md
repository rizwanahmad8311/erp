# Vendored JavaScript

Third-party JS lives here as **committed source files**. There is no npm, no
bundler and no CDN anywhere in this project — the production machine has no
internet, so anything not in git does not exist at runtime.

## How to vendor a library

1. Download the minified distribution file on a machine that has internet.
2. Save it here with its version in the filename, e.g. `htmx-2.0.4.min.js`.
3. Copy it to `static/dist/js/` (that directory is committed too).
4. Record what you added and why in the table below.
5. Reference it from a template with `{% static 'js/htmx-2.0.4.min.js' %}` —
   never a `<script src="https://...">`.

## Rules

- No `<script src>` pointing at any external host, in any template, ever.
- Pin the version in the filename so upgrades are visible in a diff.
- Prefer libraries that ship a single dependency-free file.

## Inventory

| File | Version | Source URL | Why |
| ---- | ------- | ---------- | --- |
| _(none yet)_ | | | |
