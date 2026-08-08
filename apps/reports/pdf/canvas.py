"""The canvas that can say "page 2 of 5", and the watermark it draws.

"of 5" is the awkward part. ReportLab lays a document out one page at a time and
does not know how many there will be until the last one is finished, so a footer
written during the layout can only ever say "page 2". The fix is this class: it
**defers every page** into a list instead of emitting it, and when the document
is finally saved it knows the count, replays each page, and stamps the footer
with a total it now has.

That is also where the CANCELLED watermark goes. Drawing it here rather than as
a flowable means it lands on **every** page including ones made entirely of
overflow rows, it sits under nothing and over everything, and no layout has to
remember to include it.
"""

from __future__ import annotations

from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as pdfcanvas

from .fonts import fonts
from .theme import ALARM, MUTED, RULE


class NumberedCanvas(pdfcanvas.Canvas):
    """A canvas that stamps "Page x of y" and an optional diagonal watermark.

    Built by :class:`~apps.reports.pdf.base.PDFDocument`, which passes the
    watermark text and the footer line through ``canvasmaker``.

    The cost of knowing the total is holding every page's state in memory until
    the document is saved. For an invoice or a month's ledger that is a handful
    of pages and entirely fine; it is the reason this is not used for anything
    that could run to thousands.
    """

    #: What is written across the page, or "" for no watermark.
    watermark: str = ""
    #: The company's own footer line, printed above the page number.
    footer_text: str = ""
    #: Whether to draw the signature rule at the very bottom of the last page.
    signature_label: str = ""

    def __init__(self, *args, **kwargs):
        self.watermark = kwargs.pop("watermark", "") or ""
        self.footer_text = kwargs.pop("footer_text", "") or ""
        self.signature_label = kwargs.pop("signature_label", "") or ""
        super().__init__(*args, **kwargs)
        self._saved_states = []

    def showPage(self):
        """Hold the page instead of writing it. The count is not known yet."""
        self._saved_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        """Replay every held page, now that the total is known, and write them."""
        total = len(self._saved_states)
        for state in self._saved_states:
            self.__dict__.update(state)
            self._draw_watermark()
            self._draw_footer(total)
            super().showPage()
        self._saved_states = []
        super().save()

    # ------------------------------------------------------------------
    # Furniture
    # ------------------------------------------------------------------
    def _draw_watermark(self) -> None:
        """CANCELLED, across the page, in the alarm colour.

        The paper twin of ``.doc-watermark`` in ``static/src/css/app.css``, and
        it exists for the same reason: a cancelled bill printed without the word
        CANCELLED on it is a cancelled bill somebody will pay.

        Drawn *before* the footer and after the content, at low opacity, rotated
        through the diagonal of the page so it cannot be mistaken for part of
        the document and cannot hide a figure underneath it.
        """
        if not self.watermark:
            return

        width, height = self._pagesize
        face = fonts()
        # Sized to the page rather than fixed, so A5 and A4 both get a mark that
        # spans the sheet. 0.11 of the width per character is the width of a
        # bold cap in Helvetica at that size, near enough.
        size = min(width, height) / max(len(self.watermark), 1) * 1.9

        self.saveState()
        self.translate(width / 2, height / 2)
        self.rotate(30)
        self.setFillColor(ALARM)
        self.setFillAlpha(0.13)
        self.setFont(face.bold, size)
        self.drawCentredString(0, -size / 3, self.watermark)
        self.restoreState()

    def _draw_footer(self, total: int) -> None:
        """The rule, the company's footer text, and "Page x of y"."""
        width, _height = self._pagesize
        face = fonts()
        margin = 14 * mm
        baseline = 10 * mm

        self.saveState()
        self.setStrokeColor(RULE)
        self.setLineWidth(0.4)
        self.line(margin, baseline + 7 * mm, width - margin, baseline + 7 * mm)

        self.setFillColor(MUTED)
        self.setFont(face.body, 7)
        if self.footer_text:
            self.drawString(margin, baseline + 3 * mm, self.footer_text[:160])
        self.drawRightString(
            width - margin, baseline + 3 * mm, f"Page {self._pageNumber} of {total}"
        )
        self.restoreState()


class PlainCanvas(pdfcanvas.Canvas):
    """No footer, no watermark, no page numbers — for a till roll.

    A thermal receipt is one continuous strip that the printer cuts at the end.
    "Page 1 of 1" on it is noise, and a rule 10mm from the bottom is 10mm of
    wasted paper on every single receipt of the day.
    """


__all__ = ["NumberedCanvas", "PlainCanvas"]
