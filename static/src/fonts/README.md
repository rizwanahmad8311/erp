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
