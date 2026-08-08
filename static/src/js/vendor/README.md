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

SHA-256 of `htmx-2.0.4.min.js`:
`e209dda5c8235479f3166defc7750e1dbcd5a5c1808b7792fc2e6733768fb447`

Verify a re-download against that before replacing the file. The copy in
`static/dist/js/` must stay byte-identical — `make js` copies it there.
