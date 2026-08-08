"""Which typefaces a printed page uses, and where they come from.

**Vendored, never fetched** (CLAUDE.md §7). ReportLab reads TTF files straight
off disk from ``static/src/fonts/`` — the same directory the browser's
``@font-face`` rules point at, which is the whole reason that directory's README
asks for a TTF alongside every WOFF2. Register a font here and the invoice on
screen and the invoice on paper are set in the same face.

What happens when nothing is vendored
-------------------------------------
This installation ships **no** webfont today: ``static/src/css/app.css`` sets
``--font-mono`` to a system stack and leaves the body font to Tailwind's system
stack. So the honest match for print is ReportLab's built-in Helvetica and
Courier, which is what a system sans and a system mono resolve to on the office
PC, and that is the documented fallback below.

Drop ``Inter-Regular.ttf`` and friends into ``static/src/fonts/`` and set
``PDF_FONT_FAMILY``, and every PDF picks them up with no code change. Nothing
here downloads anything, ever.

Urdu
----
The amount in words can be printed in Urdu as well as English
(:func:`apps.core.words.amount_in_words_urdu`), but only if a vendored font
actually has Arabic-script glyphs. :func:`fonts` reports whether one does, and
the invoice prints the English line alone when none is — a row of empty boxes on
a bill is worse than no second line.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple

from django.conf import settings
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFError, TTFont

logger = logging.getLogger(__name__)

#: Where vendored TTFs live. The same directory the browser reads WOFF2 from.
FONT_DIR = Path(settings.BASE_DIR) / "static" / "src" / "fonts"

#: The four files a family is looked for under, e.g. ``Inter-Regular.ttf``.
#: One convention, so adding a family is a drag-and-drop rather than a code
#: change.
_SUFFIXES = {
    "regular": "Regular",
    "bold": "Bold",
    "italic": "Italic",
    "bold_italic": "BoldItalic",
}

#: ReportLab's built-in Type 1 faces. Always present, embedded in nothing, and
#: rendered by every PDF viewer ever written — which is exactly what a fallback
#: has to be on a machine with no internet and no font installer.
BUILTIN_BODY = ("Helvetica", "Helvetica-Bold", "Helvetica-Oblique", "Helvetica-BoldOblique")
BUILTIN_MONO = ("Courier", "Courier-Bold")

#: A character that only an Arabic-script font can draw. ARABIC LETTER ALEF —
#: which does look like a Latin "l" in a source file, hence the noqa; it is the
#: whole point that it is not one.
_URDU_PROBE = "ا"  # noqa: RUF001


class Fonts(NamedTuple):
    """The four faces a page is set in, resolved once at import time.

    ``mono`` carries the numbers. Every amount column on every layout here uses
    it, because a column of figures that does not line up digit-for-digit is a
    column somebody adds up wrong — the same reason ``.amount`` on screen sets
    ``font-variant-numeric: tabular-nums``.
    """

    body: str
    bold: str
    italic: str
    mono: str
    mono_bold: str
    #: Whether ``body`` can actually draw Urdu. False on the built-in fallback.
    has_urdu: bool
    #: True when the faces came off disk rather than out of ReportLab.
    is_vendored: bool


def _family_files(family: str) -> dict[str, Path]:
    """The TTFs present on disk for a family, keyed by weight."""
    found = {}
    for weight, suffix in _SUFFIXES.items():
        for extension in (".ttf", ".otf"):
            candidate = FONT_DIR / f"{family}-{suffix}{extension}"
            if candidate.is_file():
                found[weight] = candidate
                break
    return found


def _register_family(family: str) -> tuple[str, str, str] | None:
    """Register a vendored family and return ``(regular, bold, italic)`` names.

    ``None`` when the family is not on disk, or when a file that is there turns
    out not to be a usable TTF — a corrupt font must not stop an invoice from
    printing, so it is logged and the caller falls back.
    """
    files = _family_files(family)
    if "regular" not in files:
        return None

    names = {}
    try:
        for weight, path in files.items():
            name = f"{family}-{_SUFFIXES[weight]}"
            pdfmetrics.registerFont(TTFont(name, str(path)))
            names[weight] = name
    except (TTFError, OSError) as exc:
        logger.warning("Could not register the vendored font %s: %s", family, exc)
        return None

    regular = names["regular"]
    bold = names.get("bold", regular)
    italic = names.get("italic", regular)
    bold_italic = names.get("bold_italic", bold)

    # So <b> and <i> inside a Paragraph pick the right file rather than letting
    # ReportLab synthesise a slant.
    pdfmetrics.registerFontFamily(
        family, normal=regular, bold=bold, italic=italic, boldItalic=bold_italic
    )
    return regular, bold, italic


def _can_draw(font_name: str, text: str) -> bool:
    """Whether a registered font has a glyph for every character in ``text``.

    Asked before printing the Urdu line. ReportLab does not refuse to draw a
    missing glyph — it draws nothing, or a box, which is how a bill goes out
    with a row of squares where an amount should be.
    """
    try:
        face = pdfmetrics.getFont(font_name).face
    except KeyError:  # pragma: no cover - only for a name never registered
        return False
    mapping = getattr(face, "charToGlyph", None)
    if mapping is None:
        return False  # a built-in Type 1 face: Latin-1 and nothing else
    return all(ord(character) in mapping for character in text)


@lru_cache(maxsize=1)
def fonts() -> Fonts:
    """Register the vendored faces once and say what is available.

    Cached: registering the same TTF on every invoice would re-parse the file
    each time, and ReportLab keeps its own global registry anyway.
    """
    body_family = getattr(settings, "PDF_FONT_FAMILY", "")
    mono_family = getattr(settings, "PDF_MONO_FONT_FAMILY", "")

    body = _register_family(body_family) if body_family else None
    mono = _register_family(mono_family) if mono_family else None

    if body is None:
        if body_family:
            logger.info(
                "PDF_FONT_FAMILY is %r but no %s-Regular.ttf is in %s; printing in Helvetica.",
                body_family,
                body_family,
                FONT_DIR,
            )
        regular, bold, italic = BUILTIN_BODY[0], BUILTIN_BODY[1], BUILTIN_BODY[2]
        vendored = False
    else:
        regular, bold, italic = body
        vendored = True

    if mono is None:
        mono_regular, mono_bold = BUILTIN_MONO
    else:
        mono_regular, mono_bold = mono[0], mono[1]

    return Fonts(
        body=regular,
        bold=bold,
        italic=italic,
        mono=mono_regular,
        mono_bold=mono_bold,
        has_urdu=_can_draw(regular, _URDU_PROBE),
        is_vendored=vendored,
    )


def reset() -> None:
    """Forget the cached resolution. For tests that vendor a font mid-run."""
    fonts.cache_clear()


__all__ = ["FONT_DIR", "Fonts", "fonts", "reset"]
