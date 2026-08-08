"""The template every printed page is built on.

One class, :class:`PDFDocument`, which owns the five things that are the same on
every document this system prints:

    company header block      name, logo, address, phone, NTN — on every page
    document title            what this piece of paper is
    meta block                code, date, party, status, "Amends: SI-…"
    the content               a line table and a totals block, or a ledger
    footer                    company footer text, "Page x of y", signature rule

The header is drawn on **every** page, not just the first, because an invoice
that runs to a second sheet and arrives at a shop without a letterhead on it is
an invoice that gets queried.

Nothing here touches the database beyond the company profile, and nothing here
computes money. The renderers hand it finished strings.
"""

from __future__ import annotations

import io

from reportlab.lib.units import mm
from reportlab.platypus import BaseDocTemplate, Frame, PageTemplate

from apps.reports.models import CompanyProfile

from .blocks import HEADER_CONTENT_GAP, draw_page_header, header_depth
from .canvas import NumberedCanvas
from .theme import BOTTOM_MARGIN, MARGIN, TOP_MARGIN, page_size


class PDFDocument(BaseDocTemplate):
    """A paginated document with a repeating company header and a numbered footer.

    Usage is always the same three lines::

        document = PDFDocument(title="Sales Invoice SI-2026-000123", ...)
        document.build_story(story)
        return document.getvalue()

    ``watermark`` is the only decoration it takes, and it is passed straight to
    :class:`~apps.reports.pdf.canvas.NumberedCanvas` — see there for why the
    watermark and the page count are the same problem.
    """

    def __init__(
        self,
        *,
        title: str,
        subject: str = "",
        paper: str = "a4",
        watermark: str = "",
        profile: CompanyProfile | None = None,
        signature_label: str = "",
        header_title: str = "",
        header_meta: list[tuple[str, str]] | None = None,
    ):
        self.buffer = io.BytesIO()
        self.profile = profile if profile is not None else CompanyProfile.get()
        self.watermark = watermark
        self.signature_label = signature_label
        self.header_title = header_title or title
        self.header_meta = header_meta or []

        size = page_size(paper)
        # Measured, not assumed. A fixed top margin is wrong the moment a
        # document type carries one meta pair more than the last one did, and
        # the letterhead prints straight through the first rows of the table —
        # see apps/reports/pdf/blocks.py::header_depth.
        top_margin = max(
            header_depth(self.profile, self.header_meta) + HEADER_CONTENT_GAP, TOP_MARGIN
        )
        super().__init__(
            self.buffer,
            pagesize=size,
            leftMargin=MARGIN,
            rightMargin=MARGIN,
            topMargin=top_margin,
            bottomMargin=BOTTOM_MARGIN,
            title=title,
            subject=subject,
            author=self.profile.name or "Distribution ERP",
            creator="Distribution ERP",
        )

        frame = Frame(
            self.leftMargin,
            self.bottomMargin,
            self.width,
            self.height,
            id="content",
            leftPadding=0,
            rightPadding=0,
            topPadding=0,
            bottomPadding=0,
        )
        self.addPageTemplates([PageTemplate(id="main", frames=[frame], onPage=self._on_page)])

    # ------------------------------------------------------------------
    def _on_page(self, canvas, document) -> None:
        """The letterhead, on every page."""
        draw_page_header(
            canvas,
            document,
            profile=self.profile,
            title=self.header_title,
            meta=self.header_meta,
        )

    def build_story(self, story) -> None:
        """Lay the flowables out. The footer and watermark come from the canvas."""

        def canvasmaker(*args, **kwargs):
            return NumberedCanvas(
                *args,
                watermark=self.watermark,
                footer_text=self.profile.footer_text.replace("\n", "  ·  ").strip(),
                signature_label=self.signature_label,
                **kwargs,
            )

        self.build(story, canvasmaker=canvasmaker)

    def getvalue(self) -> bytes:
        return self.buffer.getvalue()


class ThermalDocument(BaseDocTemplate):
    """A receipt on a till roll: one narrow strip, no page furniture.

    Deliberately **not** a :class:`PDFDocument`. A roll has no page, so a
    repeating letterhead, a footer rule and "Page 1 of 1" are all wrong on it —
    the header is part of the content and prints once, at the top, and the strip
    ends when the receipt does.

    The height is an estimate the caller supplies. Too tall wastes a little
    paper before the cut; too short truncates, so the estimators in
    :mod:`apps.reports.pdf.receipts` deliberately over-allow.
    """

    def __init__(self, *, title: str, width_mm: float, height_mm: float):
        self.buffer = io.BytesIO()
        padding = 3 * mm
        super().__init__(
            self.buffer,
            pagesize=(width_mm * mm, height_mm * mm),
            leftMargin=padding,
            rightMargin=padding,
            topMargin=padding,
            bottomMargin=padding,
            title=title,
            creator="Distribution ERP",
        )
        frame = Frame(
            self.leftMargin,
            self.bottomMargin,
            self.width,
            self.height,
            id="roll",
            leftPadding=0,
            rightPadding=0,
            topPadding=0,
            bottomPadding=0,
        )
        self.addPageTemplates([PageTemplate(id="roll", frames=[frame])])

    def build_story(self, story) -> None:
        from .canvas import PlainCanvas

        self.build(story, canvasmaker=PlainCanvas)

    def getvalue(self) -> bytes:
        return self.buffer.getvalue()


__all__ = ["PDFDocument", "ThermalDocument"]
