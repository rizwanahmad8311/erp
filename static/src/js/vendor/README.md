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
| `htmx-2.0.4.min.js` | 2.0.4 | `https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js` | Server-rendered partials for the keyboard-driven entry screens. Money is recalculated on the server on every change and swapped back as HTML, so the browser never does arithmetic on paisa. |
| `alpine-3.15.12.min.js` | 3.15.12 | npm `alpinejs@3.15.12`, file `dist/cdn.min.js` | Small bits of local UI state that are not worth a server round trip — a disclosure, a menu, a confirm. It never holds a monetary value: anything that adds up is htmx-swapped from the server. |

SHA-256:

| File | SHA-256 |
| ---- | ------- |
| `htmx-2.0.4.min.js` | `e209dda5c8235479f3166defc7750e1dbcd5a5c1808b7792fc2e6733768fb447` |
| `alpine-3.15.12.min.js` | `57b37d7cae9a27d965fdae4adcc844245dfdc407e655aee85dcfff3a08036a3f` |

Verify a re-download against that before replacing the file. The copy in
`static/dist/js/` must stay byte-identical — `make js` (or `make css-mac`)
copies it there.

## A note on the source URLs above

They are recorded so an upgrade can be checked against the same origin. They are
**documentation, not references** — nothing at runtime fetches them, and
`tests/test_project_setup.py::TestNoExternalAssets` scans the templates and the
`.css`/`.js` under `static/`, not this file. If you ever paste one of these into
a template, that test is what will stop you.
