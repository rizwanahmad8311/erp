"""Page sizes, colours and paragraph styles. One place, so print matches screen.

Every colour here is the sRGB conversion of an ``oklch()`` token in
``static/src/css/app.css``. They are written out as hex rather than converted at
run time because the CSS is the source of truth and it is compiled ahead of
time — a converter here would be a second implementation of a colour space that
nobody would notice drifting.

    --color-ink      #1a1d21
    --color-muted    #6e7178
    --color-rule     #dddcd6
    --color-paper    #fbfbf9
    --color-signal   #0b5d51   <- deep pine: primary actions, posted state
    --color-alarm    #a3341f   <- rust: cancelled documents, reversing entries

Six colours and no more. Rust is reserved for reversals and cancellations, on
paper for the same reason it is on screen: an accountant scanning a printed
ledger has to find the reversals without reading every line.

Anything that changes in the CSS changes here too, and
``tests/test_pdf.py::TestTheme`` keeps the two lists the same length.
"""

from __future__ import annotations

from django.conf import settings
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4, A5, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm

from .fonts import fonts

# ---------------------------------------------------------------------------
# Colour
# ---------------------------------------------------------------------------
#: Deep pine. Primary actions on screen; a posted document on paper.
SIGNAL = colors.HexColor("#0b5d51")
#: Rust. Cancelled documents, reversing entries, overdue money, an
#: out-of-balance posting, and the CANCELLED watermark. Nothing else.
ALARM = colors.HexColor("#a3341f")

INK = colors.HexColor("#1a1d21")
MUTED = colors.HexColor("#6e7178")
RULE = colors.HexColor("#dddcd6")
PAPER = colors.HexColor("#fbfbf9")

#: Kept as aliases so the block renderers do not all have to change at once.
#: There is no separate brand or draft colour any more: a draft is *outlined* on
#: screen because it has written nothing to any ledger, and the printed
#: equivalent of an outline is muted ink.
BRAND = SIGNAL
POSTED = SIGNAL
DRAFT = MUTED
HAIRLINE = RULE
BAND = PAPER

#: What a status badge is printed in, matching ``|doc_status`` on screen.
STATUS_COLOURS = {"DRAFT": DRAFT, "POSTED": POSTED, "CANCELLED": ALARM}


# ---------------------------------------------------------------------------
# Paper
# ---------------------------------------------------------------------------
#: A4 portrait is the default because that is what the office buys. A5 exists
#: for a delivery-book invoice; landscape A4 for a ledger with many columns.
PAGE_SIZES = {
    "a4": A4,
    "a4-landscape": landscape(A4),
    "a5": A5,
}

#: The thermal roll. 80mm is the common till printer; the printable width is
#: narrower than the paper, and 72mm is what almost every 80mm head actually
#: covers. Height is not a page size on a roll — see :func:`thermal_page_size`.
THERMAL_WIDTHS_MM = {"80mm": 72, "58mm": 48}

MARGIN = 14 * mm
#: Room for the footer rule, the page number and the signature line.
BOTTOM_MARGIN = 22 * mm
#: Room for the company header block, which is drawn on every page.
TOP_MARGIN = 34 * mm


def page_size(name: str = "a4"):
    """A named paper size, or A4 for anything unrecognised.

    Forgiving on purpose: this is reached from a query string, and an invoice
    printing on the wrong paper is better than an invoice not printing.
    """
    return PAGE_SIZES.get((name or "").lower(), A4)


def thermal_page_size(width_mm: int, height_mm: float):
    """A roll-printer page: fixed width, and whatever height the content needs.

    A till roll has no page length — the printer cuts when the job ends — so the
    "page" is made tall enough for the receipt and the printer's own driver
    trims it. Getting this wrong the other way, by using A4 and letting the
    content wrap, is what produces a receipt with one word per line.
    """
    return (width_mm * mm, height_mm * mm)


# ---------------------------------------------------------------------------
# Type
# ---------------------------------------------------------------------------
def styles() -> dict[str, ParagraphStyle]:
    """The paragraph styles every layout draws from.

    Built on each call rather than at import: the font resolution behind them
    depends on what is vendored in ``static/src/fonts/``, and a test that drops
    a font in mid-run should see it — see :func:`apps.reports.pdf.fonts.reset`.
    """
    face = fonts()
    base = ParagraphStyle(
        "body",
        fontName=face.body,
        fontSize=9,
        leading=11.5,
        textColor=INK,
    )
    return {
        "body": base,
        "small": ParagraphStyle("small", parent=base, fontSize=7.5, leading=9.5, textColor=MUTED),
        "company": ParagraphStyle(
            "company", parent=base, fontName=face.bold, fontSize=15, leading=18, textColor=INK
        ),
        "title": ParagraphStyle(
            "title",
            parent=base,
            fontName=face.bold,
            fontSize=13,
            leading=16,
            alignment=TA_RIGHT,
            textColor=BRAND,
        ),
        "label": ParagraphStyle(
            "label", parent=base, fontSize=7, leading=9, textColor=MUTED, alignment=TA_LEFT
        ),
        "cell": ParagraphStyle("cell", parent=base, fontSize=8, leading=10),
        "cell_right": ParagraphStyle(
            "cell_right", parent=base, fontSize=8, leading=10, alignment=TA_RIGHT
        ),
        "amount": ParagraphStyle(
            "amount", parent=base, fontName=face.mono, fontSize=8, leading=10, alignment=TA_RIGHT
        ),
        "words": ParagraphStyle("words", parent=base, fontSize=8.5, leading=11, textColor=INK),
        "footer": ParagraphStyle(
            "footer", parent=base, fontSize=7, leading=9, textColor=MUTED, alignment=TA_CENTER
        ),
        "thermal": ParagraphStyle(
            "thermal", parent=base, fontSize=7.5, leading=9.5, alignment=TA_CENTER
        ),
        "thermal_left": ParagraphStyle("thermal_left", parent=base, fontSize=7.5, leading=9.5),
    }


# ---------------------------------------------------------------------------
# Which layout a receipt prints on
# ---------------------------------------------------------------------------
def receipt_layout(name: str | None = None) -> str:
    """The receipt layout to use, from the request or from settings.

    The office has more than one printer — an A4 laser for filing and an 80mm
    roll at the counter — so this is configuration, not a constant. Settings
    hold the default (``RECEIPT_LAYOUT``, overridable from ``.env``) and a
    ``?layout=`` on the URL picks a different printer for one job.
    """
    wanted = (name or "").strip().lower()
    if wanted in RECEIPT_LAYOUTS:
        return wanted
    default = str(getattr(settings, "RECEIPT_LAYOUT", "a5")).strip().lower()
    return default if default in RECEIPT_LAYOUTS else "a5"


#: Every receipt layout this system can print, and what each one is.
RECEIPT_LAYOUTS = {
    "a4": "A4 sheet",
    "a5": "A5 half sheet",
    "80mm": "80mm thermal roll",
    "58mm": "58mm thermal roll",
}


def is_thermal(layout: str) -> bool:
    return layout in THERMAL_WIDTHS_MM


__all__ = [
    "ALARM",
    "BAND",
    "BOTTOM_MARGIN",
    "BRAND",
    "DRAFT",
    "HAIRLINE",
    "INK",
    "MARGIN",
    "MUTED",
    "PAGE_SIZES",
    "POSTED",
    "RECEIPT_LAYOUTS",
    "RULE",
    "STATUS_COLOURS",
    "THERMAL_WIDTHS_MM",
    "TOP_MARGIN",
    "is_thermal",
    "page_size",
    "receipt_layout",
    "styles",
    "thermal_page_size",
]
