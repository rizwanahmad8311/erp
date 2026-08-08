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

**Nothing is vendored today.** `static/src/css/app.css` uses system font stacks,
so the PDFs use ReportLab's built-in Helvetica and Courier, which is what those
stacks resolve to on the office PC. That is the honest match; vendor a font in
both places when print and screen need to be identical rather than merely alike.

Amounts always print in the mono face, vendored or not: a column of figures that
does not line up digit-for-digit is a column somebody adds up wrong.

## Urdu

The amount in words can print in Urdu under the English line, but only if the
vendored body font actually has Arabic-script glyphs — `fonts.py` probes for one
and prints the English line alone when there is none. A row of empty boxes on a
bill is worse than one language. Noto Naskh Arabic is the usual choice; drop
`NotoNaskhArabic-Regular.ttf` here and point `PDF_FONT_FAMILY` at it.
