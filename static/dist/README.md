# Committed compiled output

Everything in this directory is generated from `static/src` and **committed to
git on purpose**. The Windows production machine has no node and no internet;
it serves these files as-is through WhiteNoise.

- `app.css` — Tailwind output, regenerate with `make css`
- `js/` — copied from `static/src/js` (first-party + vendored)
- `fonts/` — copied from `static/src/fonts`

Do not edit files here by hand: edit `static/src` and run `make css`.
