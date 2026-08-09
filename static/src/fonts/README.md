# Vendored fonts

Drop `.woff2` files here and reference them from `static/src/css/app.css` with a
relative `@font-face` `src: url("../fonts/<file>.woff2")`.

Copy the same files to `static/dist/fonts/` — that directory is committed, and
it is what WhiteNoise serves in production.

Never use Google Fonts, `@import url(...)`, or any other external host: the
production PC has no internet and the page would render with a fallback font
and a several-second stall on every request.

Reportlab PDFs use their own font registration (`pdfmetrics.registerFont`) and
read the same `.ttf`/`.otf` files from this directory — keep a TTF alongside the
WOFF2 if a font is needed in both the browser and printed invoices.

## What the PDF renderer looks for

`apps/reports/pdf/fonts.py` looks for the family named in `PDF_FONT_FAMILY` (and
`PDF_MONO_FONT_FAMILY`) under exactly these four names:

    <Family>-Regular.ttf
    <Family>-Bold.ttf
    <Family>-Italic.ttf
    <Family>-BoldItalic.ttf

Only `-Regular` is required; the others fall back to it. Drop the files in, set
the setting, and every PDF picks them up — there is no code change and nothing
is downloaded.

**The screen is vendored; the PDFs are not.** `static/src/css/app.css`
`@font-face`s the seven WOFF2 files below, so the browser is fully self-hosted.
ReportLab cannot read WOFF2 — it needs a TTF or OTF — and the Fontsource
packages ship only `woff2` and `woff`, so the PDFs still use the built-in
Helvetica and Courier.

That mismatch is deliberate rather than overlooked. Closing it means converting
WOFF2 to TTF (`fonttools` + `brotli`) and committing three more files, and the
brief this styling came from is a *screen* brief. Print and screen are alike,
not identical. If they need to be identical, drop `Inter-Regular.ttf`,
`Inter-Bold.ttf` and `IBMPlexMono-Regular.ttf` in here and set
`PDF_FONT_FAMILY` / `PDF_MONO_FONT_FAMILY` — no code changes.

## What is vendored

Downloaded from the Fontsource npm packages (`@fontsource/inter`,
`@fontsource/inter-tight`, `@fontsource/ibm-plex-mono`, all 5.3.0), latin subset
only, and only the weights actually used — seven files, ~147KB.

| File | SHA-256 |
| ---- | ------- |
| `inter-latin-400-normal.woff2` | `8909904ab6c872eb994093482a88a28eca2cd95912d7b6fecd72103b0dc07edc` |
| `inter-latin-500-normal.woff2` | `f3779f1efccc4bdcdf9c0a02ab95bf6bd092ed09c48c08cedc725889edd1d19f` |
| `inter-latin-600-normal.woff2` | `f9a06e79cd3a2a20951c0f0e28f66dd0e6d3fda73911d640a2125c8fcb78f21a` |
| `inter-tight-latin-500-normal.woff2` | `6c0019f88d5bc179f2c972998ed8c14e779254566da9081c5e29f364a0a0aeb6` |
| `inter-tight-latin-600-normal.woff2` | `db1a039d03ed646ef6a899f9ff92bf2f6fe382a49f1b0992066e85caf88b5be9` |
| `ibm-plex-mono-latin-400-normal.woff2` | `08949f728dc52d528e69b1667d15c89a5686a4ee9a296ff90983985f99c380f7` |
| `ibm-plex-mono-latin-500-normal.woff2` | `01d285447409c8a588692162439a038b8cbd7871309ee20267b0d2d91c6e8e22` |

Three roles, and no more: **Inter Tight** for headings, **Inter** for body and
form fields, **IBM Plex Mono** for every number, document code and date.

Note that the `src:` paths in `app.css` are relative to the compiled **output**
(`static/dist/app.css`), not to the source file — `url("fonts/x.woff2")`, not
`url("../fonts/x.woff2")`. Tailwind copies them through verbatim.

Amounts always print in the mono face, vendored or not: a column of figures that
does not line up digit-for-digit is a column somebody adds up wrong.

## Urdu

The amount in words can print in Urdu under the English line, but only if the
vendored body font actually has Arabic-script glyphs — `fonts.py` probes for one
and prints the English line alone when there is none. A row of empty boxes on a
bill is worse than one language. Noto Naskh Arabic is the usual choice; drop
`NotoNaskhArabic-Regular.ttf` here and point `PDF_FONT_FAMILY` at it.
