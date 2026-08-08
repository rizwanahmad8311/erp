"""The pieces every printed document is assembled from.

Six of them, and no renderer builds any of these by hand:

    draw_page_header    company letterhead + title + meta, drawn on every page
    line_table          the numbered line grid, with its column rules
    totals_block        subtotal / discount / tax / total, right-aligned
    amount_in_words_block   the sentence under the total
    signature_block     the ruled line somebody signs
    notes_block         terms and remarks

Two rules hold throughout. **Every numeric column is right-aligned and set in
the mono face**, so a column of figures lines up digit for digit — the paper
equivalent of ``.amount { font-variant-numeric: tabular-nums }`` on screen.
And **nothing here computes money**: every amount arrives as a formatted string
from the renderer, which got it from the document or the ledger.
"""

from __future__ import annotations

import logging

from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import Paragraph, Spacer, Table, TableStyle

from apps.core.money import fmt
from apps.core.words import amount_in_words, amount_in_words_urdu

from .fonts import fonts
from .theme import (
    ALARM,
    BAND,
    BRAND,
    HAIRLINE,
    INK,
    MARGIN,
    MUTED,
    RULE,
    STATUS_COLOURS,
    styles,
)

logger = logging.getLogger(__name__)

#: How much of the header band the logo may take. Wider than this and the
#: company name has nowhere to sit.
LOGO_MAX_WIDTH = 38 * mm
LOGO_MAX_HEIGHT = 16 * mm

# The header's geometry, in one place. :func:`header_depth` and
# :func:`draw_page_header` both read these, which is what stops the letterhead
# growing past the top of the content frame and printing through the first rows
# of the line table — the document with eight meta pairs is the one that would
# have found out.
HEADER_NAME_DROP = 4.5 * mm  # baseline of the company name, below the margin
HEADER_FIRST_LINE = 9 * mm  # baseline of the first detail line and meta row
HEADER_LINE_STEP = 3.4 * mm  # between the company's address lines
HEADER_META_STEP = 3.8 * mm  # between the meta pairs on the right
HEADER_RULE_GAP = 2 * mm  # from the last line down to the rule
#: Between the rule and the first flowable of the story.
HEADER_CONTENT_GAP = 4 * mm
#: Where the meta labels sit, measured in from the right margin.
HEADER_META_LABEL_INSET = 34 * mm


def _header_lines(profile, meta):
    """The company lines and the meta pairs that will actually be drawn.

    Blank ones are dropped here rather than in two places, so the depth
    calculation counts exactly the rows the drawing puts on the page.
    """
    company = [
        text
        for text in (*profile.address_lines[:2], profile.contact_line, profile.tax_line)
        if text
    ]
    visible_meta = [(label, value) for label, value in meta if value not in (None, "")]
    return company, visible_meta


def header_depth(profile, meta) -> float:
    """From the top of the page down to the bottom of the header rule.

    The document's top margin is set from this (see
    :class:`~apps.reports.pdf.base.PDFDocument`), so the letterhead and the
    content can never collide however many meta pairs a document type carries.
    """
    company, visible_meta = _header_lines(profile, meta)
    company_bottom = HEADER_FIRST_LINE + max(len(company) - 1, 0) * HEADER_LINE_STEP
    meta_bottom = HEADER_FIRST_LINE + max(len(visible_meta) - 1, 0) * HEADER_META_STEP
    logo_bottom = LOGO_MAX_HEIGHT if profile.logo_file() else 0
    return MARGIN + max(company_bottom, meta_bottom, logo_bottom) + HEADER_RULE_GAP


# ===========================================================================
# The letterhead
# ===========================================================================
def draw_page_header(canvas, document, *, profile, title: str, meta) -> None:
    """The company block, the document title and the meta pairs, at the top.

    Drawn directly on the canvas rather than as a flowable because it repeats on
    every page — a flowable would appear once, at the start of the story, and
    page two of a long invoice would arrive with no letterhead on it.

    ``meta`` is a list of ``(label, value)`` pairs. Whatever the renderer wants
    seen at a glance: the code, the date, the party, the status, and — on an
    amendment — which document it replaces.
    """
    width, height = document.pagesize
    face = fonts()
    top = height - MARGIN
    company, visible_meta = _header_lines(profile, meta)

    canvas.saveState()

    left = MARGIN
    if _draw_logo(canvas, profile, x=left, top=top):
        left += LOGO_MAX_WIDTH + 4 * mm

    # -- the company ---------------------------------------------------------
    canvas.setFillColor(INK)
    canvas.setFont(face.bold, 14)
    canvas.drawString(left, top - HEADER_NAME_DROP, profile.name or "— company profile not set —")

    canvas.setFont(face.body, 7.5)
    canvas.setFillColor(MUTED)
    for index, text in enumerate(company):
        canvas.drawString(left, top - HEADER_FIRST_LINE - index * HEADER_LINE_STEP, text)

    # -- the title and the meta pairs ---------------------------------------
    canvas.setFillColor(BRAND)
    canvas.setFont(face.bold, 13)
    canvas.drawRightString(width - MARGIN, top - HEADER_NAME_DROP, title.upper())

    for index, (label, value) in enumerate(visible_meta):
        baseline = top - HEADER_FIRST_LINE - index * HEADER_META_STEP
        canvas.setFont(face.body, 7)
        canvas.setFillColor(MUTED)
        canvas.drawRightString(width - MARGIN - HEADER_META_LABEL_INSET, baseline, str(label))
        canvas.setFont(face.mono, 8)
        # A status reads in its own colour, the same three the screen uses.
        canvas.setFillColor(STATUS_COLOURS.get(str(value), INK))
        canvas.drawRightString(width - MARGIN, baseline, str(value))

    rule_y = height - header_depth(profile, meta)
    canvas.setStrokeColor(RULE)
    canvas.setLineWidth(0.8)
    canvas.line(MARGIN, rule_y, width - MARGIN, rule_y)

    canvas.restoreState()


def _draw_logo(canvas, profile, *, x: float, top: float) -> bool:
    """Draw the logo if there is one that can be read. Never raises.

    An invoice must print. A logo that has been deleted off disk, or saved in a
    format this machine's imaging library cannot open, is a cosmetic problem —
    stopping the print over it would be a real one — so this logs and returns
    False, and the header falls back to the company name alone.

    The file is always local (:meth:`apps.reports.models.CompanyProfile.logo_file`
    returns a path under MEDIA_ROOT or nothing). Nothing here fetches a URL.
    """
    path = profile.logo_file()
    if path is None:
        return False

    try:
        image = ImageReader(str(path))
        image_width, image_height = image.getSize()
    except Exception as exc:
        logger.warning("Could not read the company logo at %s: %s", path, exc)
        return False

    if not image_width or not image_height:
        return False

    scale = min(LOGO_MAX_WIDTH / image_width, LOGO_MAX_HEIGHT / image_height)
    drawn_width, drawn_height = image_width * scale, image_height * scale
    canvas.drawImage(
        image,
        x,
        top - drawn_height,
        width=drawn_width,
        height=drawn_height,
        mask="auto",
        preserveAspectRatio=True,
    )
    return True


# ===========================================================================
# The line grid
# ===========================================================================
def line_table(columns, rows, *, available_width: float, zebra: bool = True) -> Table:
    """The numbered line grid.

    ``columns`` is a list of ``(heading, width_ratio, align)``. The ratios are
    scaled to whatever width the frame actually has, so the same column spec
    lays out on A4 and on A5 without a second set of numbers to keep in step.

    ``align`` is ``"l"``, ``"r"`` or ``"c"``. Every ``"r"`` column is set in the
    mono face by the caller through :func:`money_cell` / :func:`qty_cell`, which
    is what makes a column of amounts add up by eye.
    """
    face = fonts()
    total_ratio = sum(ratio for _heading, ratio, _align in columns)
    widths = [available_width * ratio / total_ratio for _h, ratio, _a in columns]

    header = [Paragraph(f"<b>{heading}</b>", styles()["label"]) for heading, _r, _a in columns]

    table = Table([header, *rows], colWidths=widths, repeatRows=1, hAlign="LEFT")

    style = [
        ("FONTNAME", (0, 0), (-1, -1), face.body),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        # The head band, and the rule under it.
        ("BACKGROUND", (0, 0), (-1, 0), BAND),
        ("LINEBELOW", (0, 0), (-1, 0), 0.7, RULE),
        ("LINEBELOW", (0, 1), (-1, -2), 0.25, HAIRLINE),
        ("LINEBELOW", (0, -1), (-1, -1), 0.7, RULE),
    ]
    for index, (_heading, _ratio, align) in enumerate(columns):
        style.append(
            ("ALIGN", (index, 0), (index, -1), {"l": "LEFT", "r": "RIGHT"}.get(align, "CENTER"))
        )

    if zebra:
        # Every other row, faintly. On a 40-line invoice this is the difference
        # between reading across a row and reading across two.
        for row_index in range(1, len(rows) + 1):
            if row_index % 2 == 0:
                style.append(
                    ("BACKGROUND", (0, row_index), (-1, row_index), colors.HexColor("#fafafa"))
                )

    table.setStyle(TableStyle(style))
    return table


def money_cell(paisa: int, *, bold: bool = False, alarm: bool = False) -> Paragraph:
    """A right-aligned amount in the mono face. ``None`` renders blank."""
    if paisa is None:
        return Paragraph("", styles()["amount"])
    style = styles()["amount"]
    text = fmt(paisa)
    if bold:
        text = f"<b>{text}</b>"
    if alarm:
        text = f'<font color="#bd413f">{text}</font>'
    return Paragraph(text, style)


def qty_cell(text: str) -> Paragraph:
    """A right-aligned quantity, already formatted as "3 ctn + 5 pcs"."""
    return Paragraph(text or "", styles()["amount"])


def text_cell(text: str, *, small: bool = False) -> Paragraph:
    return Paragraph(str(text or ""), styles()["small" if small else "cell"])


# ===========================================================================
# The totals
# ===========================================================================
def totals_block(pairs, *, available_width: float) -> Table:
    """Subtotal / discount / tax / total, right-aligned against the line grid.

    ``pairs`` is ``[(label, paisa, is_total)]``. The row flagged ``is_total``
    gets the rule above it and the heavier face — there is exactly one, and it
    is the figure the shop pays.
    """
    face = fonts()
    rows = []
    total_row = None
    for index, (label, paisa, is_total) in enumerate(pairs):
        rows.append(
            [
                Paragraph(f"<b>{label}</b>" if is_total else label, styles()["cell_right"]),
                money_cell(paisa, bold=is_total),
            ]
        )
        if is_total:
            total_row = index

    label_width = available_width * 0.62
    table = Table(rows, colWidths=[label_width, available_width - label_width], hAlign="RIGHT")

    style = [
        ("FONTNAME", (0, 0), (-1, -1), face.body),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
    ]
    if total_row is not None:
        style += [
            ("LINEABOVE", (0, total_row), (-1, total_row), 0.8, INK),
            ("LINEBELOW", (0, total_row), (-1, total_row), 1.2, INK),
            ("TOPPADDING", (0, total_row), (-1, total_row), 4),
            ("BOTTOMPADDING", (0, total_row), (-1, total_row), 4),
        ]
    table.setStyle(TableStyle(style))
    return table


def amount_in_words_block(paisa: int, *, available_width: float) -> Table:
    """The sentence under the total: "Rupees One Lakh … and Fifty Paisa Only".

    Lakh and crore, not million — see :mod:`apps.core.words`. A cheque or a bill
    written "Ten Million" in a Karachi office is one that gets queried.

    The Urdu line is printed underneath **only when a vendored font can actually
    draw it**. With ReportLab's built-in Helvetica it cannot, and a row of empty
    boxes on a bill is worse than one language.
    """
    face = fonts()
    lines = [Paragraph(f"<b>Amount in words:</b> {amount_in_words(paisa)}", styles()["words"])]
    if face.has_urdu:
        lines.append(Paragraph(amount_in_words_urdu(paisa), styles()["words"]))

    table = Table([[line] for line in lines], colWidths=[available_width], hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("BACKGROUND", (0, 0), (-1, -1), BAND),
                ("BOX", (0, 0), (-1, -1), 0.4, RULE),
            ]
        )
    )
    return table


# ===========================================================================
# The foot of the document
# ===========================================================================
def signature_block(*, available_width: float, left: str = "", right: str = "Authorised signature"):
    """Two ruled lines with a caption under each, for the bottom of a document.

    Part of the flow rather than the page furniture: it belongs after the last
    line of the document, not 10mm from the bottom of every page, and on a
    two-page invoice it should appear once, at the end.
    """
    cell_width = available_width / 2
    rows = [
        [
            Paragraph(left or "&nbsp;", styles()["small"]),
            Paragraph(right or "&nbsp;", styles()["small"]),
        ]
    ]
    table = Table(rows, colWidths=[cell_width, cell_width], hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("LINEABOVE", (0, 0), (0, 0), 0.5, INK),
                ("LINEABOVE", (1, 0), (1, 0), 0.5, INK),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("LEFTPADDING", (0, 0), (0, 0), 0),
                ("RIGHTPADDING", (1, 0), (1, 0), 0),
                ("ALIGN", (1, 0), (1, 0), "RIGHT"),
            ]
        )
    )
    return [Spacer(1, 14 * mm), table]


def notes_block(text: str, *, available_width: float, heading: str = "") -> list:
    """Remarks or terms, printed under the totals. Empty text prints nothing."""
    text = (text or "").strip()
    if not text:
        return []
    body = text.replace("\n", "<br/>")
    if heading:
        body = f"<b>{heading}</b><br/>{body}"
    return [Spacer(1, 4 * mm), Paragraph(body, styles()["small"])]


def status_note(document) -> list:
    """A line naming the cancellation, printed under the header of a dead bill.

    The watermark says *that* it was cancelled; this says **when and why**, which
    is the thing somebody holding the paper actually needs.
    """
    if document.status != "CANCELLED":
        return []
    reason = (document.cancel_reason or "").strip()
    when = document.cancelled_at.date().isoformat() if document.cancelled_at else ""
    text = f"<b>CANCELLED</b> on {when}. Its entries have been reversed."
    if reason:
        text += f" Reason: {reason}"
    paragraph = Paragraph(f'<font color="#bd413f">{text}</font>', styles()["small"])
    table = Table([[paragraph]], colWidths=["100%"], hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 0.7, ALARM),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    return [table, Spacer(1, 4 * mm)]


__all__ = [
    "amount_in_words_block",
    "draw_page_header",
    "line_table",
    "money_cell",
    "notes_block",
    "qty_cell",
    "signature_block",
    "status_note",
    "text_cell",
    "totals_block",
]
