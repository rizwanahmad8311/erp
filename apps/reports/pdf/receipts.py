"""The money receipt — on a sheet, or on a till roll.

One function, :func:`payment_receipt_pdf`, and two layouts behind it, because
the office prints the same receipt two ways: an A5 half sheet for the file, and
an 80mm strip handed to the shopkeeper at the counter. Which one is decided by
:func:`apps.reports.pdf.theme.receipt_layout` — settings for the default,
``?layout=`` for one job.

The strip is not the sheet with narrower margins. A 72mm printable width fits
about 42 monospaced characters, so it gets its own layout: everything centred or
in two columns, no line grid, no page furniture, and the amount in words wrapped
rather than truncated — a receipt is what the shopkeeper keeps, and the figure
on it has to be readable.
"""

from __future__ import annotations

from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, Spacer, Table, TableStyle

from apps.core.enums import DocumentStatus
from apps.core.money import fmt
from apps.core.words import amount_in_words
from apps.payments.enums import PaymentDirection

from .base import PDFDocument, ThermalDocument
from .blocks import amount_in_words_block, notes_block, signature_block, status_note, totals_block
from .fonts import fonts
from .theme import ALARM, INK, RULE, THERMAL_WIDTHS_MM, is_thermal, receipt_layout, styles

#: Roughly how tall a thermal receipt comes out, before the allocation lines.
#: Deliberately generous: too tall wastes a centimetre of paper before the cut,
#: too short truncates the total off the bottom.
_THERMAL_BASE_MM = 112
_THERMAL_PER_LINE_MM = 5

#: How many characters of a name fit in the right-hand half of a 72mm roll.
#: Two wrapped lines are fine; a shop name cut mid-word is not.
_THERMAL_NAME_CHARS = 34


def _shorten(text: str, limit: int = _THERMAL_NAME_CHARS) -> str:
    """Trim a name to fit the roll, breaking on a word.

    ``"New Sabir Kiryana Store"[:22]`` gives ``"New Sabir KiryanaStor"``, which
    reads as a different shop — and on a receipt somebody keeps, the name is
    half the point. Breaking on a space and marking the cut is worse than
    printing the whole name and much better than printing a wrong one.
    """
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    clipped = text[:limit].rsplit(" ", 1)[0]
    return f"{clipped or text[:limit]}.."


def payment_receipt_pdf(payment, *, layout: str | None = None) -> bytes:
    """A receipt for money in, or a voucher for money out.

    ``layout`` is one of :data:`~apps.reports.pdf.theme.RECEIPT_LAYOUTS`. Left
    out, it comes from ``settings.RECEIPT_LAYOUT`` — which is how the counter PC
    prints 80mm and the back office prints A5 without either of them passing a
    parameter.
    """
    chosen = receipt_layout(layout)
    if is_thermal(chosen):
        return _thermal_receipt(payment, width_mm=THERMAL_WIDTHS_MM[chosen])
    return _sheet_receipt(payment, paper=chosen)


def _title(payment) -> str:
    return "Receipt" if payment.direction == PaymentDirection.RECEIVE else "Payment Voucher"


def _allocation_rows(payment):
    """Which bills this money settles, resolved once for either layout."""
    from apps.payments.services import allocation_rows

    return allocation_rows(payment)


# ===========================================================================
# The sheet
# ===========================================================================
def _sheet_receipt(payment, *, paper: str) -> bytes:
    """A5 by default: the copy that goes in the file, with a signature line."""
    title = _title(payment)
    meta = [
        ("Document", payment.code),
        ("Date", payment.posting_date.strftime("%d %b %Y")),
        ("Party", payment.party_name),
        ("Mode", payment.get_mode_display()),
    ]
    if payment.is_cheque:
        meta.append(("Cheque", payment.cheque_no))
        meta.append(("Cheque date", payment.cheque_date.strftime("%d %b %Y")))
    if payment.collected_by_id:
        meta.append(("Collected by", payment.collected_by.name))
    if payment.amended_from_id:
        meta.append(("Amends", payment.amended_from.code))
    meta.append(("Status", payment.get_status_display().upper()))

    pdf = PDFDocument(
        title=f"{title} {payment.code}",
        subject=f"{title} for {payment.party_name}",
        paper=paper,
        watermark="CANCELLED" if payment.status == DocumentStatus.CANCELLED else "",
        header_title=title,
        header_meta=meta,
    )
    width = pdf.width
    style = styles()

    received = payment.direction == PaymentDirection.RECEIVE
    preposition = "Received from" if received else "Paid to"

    story = [*status_note(payment)]
    story.append(
        Paragraph(
            f"<b>{preposition}</b> {payment.party_name} "
            f"<b>by</b> {payment.get_mode_display().lower()}"
            f"{f' — cheque {payment.cheque_no}' if payment.is_cheque else ''}.",
            style["body"],
        )
    )
    story.append(Spacer(1, 4 * mm))

    pairs = [("Amount", payment.amount_paisa, True)]
    allocations = _allocation_rows(payment)
    if allocations:
        pairs = [
            ("Applied to bills", payment.allocated_paisa, False),
            ("On account", payment.unallocated_paisa, False),
            ("Amount", payment.amount_paisa, True),
        ]
    story.append(totals_block(pairs, available_width=width * 0.5))
    story.append(Spacer(1, 4 * mm))
    story.append(amount_in_words_block(payment.amount_paisa, available_width=width))

    if allocations:
        story.append(Spacer(1, 5 * mm))
        story.append(Paragraph("<b>Applied to</b>", style["small"]))
        rows = [
            [
                Paragraph(allocation.document_code, style["cell"]),
                Paragraph(fmt(allocation.amount_paisa), style["amount"]),
            ]
            for allocation in allocations
        ]
        table = Table(rows, colWidths=[width * 0.6, width * 0.4], hAlign="LEFT")
        table.setStyle(
            TableStyle(
                [
                    ("FONTSIZE", (0, 0), (-1, -1), 8),
                    ("LINEBELOW", (0, 0), (-1, -1), 0.25, RULE),
                    ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                    ("TOPPADDING", (0, 0), (-1, -1), 2),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                ]
            )
        )
        story.append(table)

    story += notes_block(payment.remarks, available_width=width, heading="Remarks")
    story += signature_block(
        available_width=width,
        left="Received by" if not received else "Paid by",
        right="For " + (pdf.profile.name or "the company"),
    )

    pdf.build_story(story)
    return pdf.getvalue()


# ===========================================================================
# The roll
# ===========================================================================
def _thermal_receipt(payment, *, width_mm: int) -> bytes:
    """80mm (or 58mm) of till roll: the copy the shopkeeper walks away with.

    Everything is centred or in two columns and nothing is a grid. A thermal
    head at 203dpi over 72mm has about 42 monospaced characters to work with,
    and a table with eight columns in it comes out as eight columns of one
    letter each.
    """
    allocations = _allocation_rows(payment)
    height_mm = _THERMAL_BASE_MM + _THERMAL_PER_LINE_MM * len(allocations)
    if payment.status == DocumentStatus.CANCELLED:
        height_mm += 8

    pdf = ThermalDocument(
        title=f"{_title(payment)} {payment.code}",
        width_mm=width_mm,
        height_mm=height_mm,
    )
    from apps.reports.models import CompanyProfile

    profile = CompanyProfile.get()
    style = styles()
    face = fonts()
    width = pdf.width
    received = payment.direction == PaymentDirection.RECEIVE

    def centred(text: str, *, size: float = 7.5, bold: bool = False, colour=INK):
        paragraph = style["thermal"].clone("t")
        paragraph.fontSize = size
        paragraph.leading = size * 1.3
        paragraph.fontName = face.bold if bold else face.body
        paragraph.textColor = colour
        return Paragraph(text, paragraph)

    def two_column(left: str, right: str, *, bold: bool = False):
        """A label on the left and a figure on the right, mono so they align."""
        right_style = style["amount"].clone("r")
        right_style.fontName = face.mono_bold if bold else face.mono
        table = Table(
            [[Paragraph(left, style["thermal_left"]), Paragraph(right, right_style)]],
            colWidths=[width * 0.5, width * 0.5],
        )
        table.setStyle(
            TableStyle(
                [
                    ("TOPPADDING", (0, 0), (-1, -1), 1),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ]
            )
        )
        return table

    def rule():
        table = Table([[""]], colWidths=[width], rowHeights=[1])
        table.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 0.4, RULE)]))
        return table

    story = [
        centred(profile.name or "RECEIPT", size=10, bold=True),
    ]
    for line in profile.address_lines[:2]:
        story.append(centred(line, size=6.5))
    if profile.contact_line:
        story.append(centred(profile.contact_line, size=6.5))
    if profile.ntn:
        story.append(centred(f"NTN {profile.ntn}", size=6.5))

    story += [Spacer(1, 2 * mm), rule(), Spacer(1, 1.5 * mm)]
    story.append(centred(_title(payment).upper(), size=9, bold=True))

    if payment.status == DocumentStatus.CANCELLED:
        # No rotated watermark on a roll — it would be unreadable at this width
        # and the head prints it as a grey smear. A boxed line does the job.
        story.append(centred("*** CANCELLED ***", size=9, bold=True, colour=ALARM))

    story += [Spacer(1, 1.5 * mm)]
    story.append(two_column("No.", payment.code))
    story.append(two_column("Date", payment.posting_date.strftime("%d-%m-%Y")))
    story.append(two_column("Party", _shorten(payment.party_name)))
    story.append(two_column("Mode", payment.get_mode_display()))
    if payment.is_cheque:
        story.append(two_column("Cheque", payment.cheque_no))
    if payment.collected_by_id:
        story.append(two_column("By", _shorten(payment.collected_by.name)))

    story += [Spacer(1, 1.5 * mm), rule(), Spacer(1, 1.5 * mm)]

    if allocations:
        story.append(centred("APPLIED TO", size=7, bold=True))
        for allocation in allocations:
            story.append(two_column(allocation.document_code, fmt(allocation.amount_paisa)))
        if payment.unallocated_paisa:
            story.append(two_column("On account", fmt(payment.unallocated_paisa)))
        story += [Spacer(1, 1 * mm), rule(), Spacer(1, 1.5 * mm)]

    story.append(two_column("TOTAL", fmt(payment.amount_paisa), bold=True))
    story += [Spacer(1, 2 * mm)]
    story.append(centred(amount_in_words(payment.amount_paisa), size=6.5))
    story += [Spacer(1, 2 * mm), rule(), Spacer(1, 2 * mm)]

    story.append(
        centred(
            "Thank you" if received else "Payment issued",
            size=7,
        )
    )
    if profile.footer_text:
        story.append(centred(profile.footer_text.replace("\n", " ")[:120], size=6))

    pdf.build_story(story)
    return pdf.getvalue()


__all__ = ["payment_receipt_pdf"]
