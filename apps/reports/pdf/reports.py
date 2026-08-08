"""The PDF half of "three output formats": any report, one renderer.

The five renderers next door draw *documents* — an invoice has a totals block, a
signature line and an amount in words, and none of that is shared with a trial
balance. A report is the other shape: a wide table with a heading band, a totals
line, and a note or two underneath. So it gets one renderer that every
registered report goes through, driven by the same
:class:`~apps.reports.columns.Column` list the screen and the CSV use.

Landscape is a property of the report, not a preference. A report ten columns
wide does not fit on A4 portrait, and finding that out at the printer is finding
it out too late — see :attr:`apps.reports.registry.Report.landscape`.

Nothing here computes money. The rows arrive holding integer paisa and go
through :func:`apps.reports.columns.display`, which is the same function the
HTML table calls — so a figure cannot read one way on screen and another on
paper.
"""

from __future__ import annotations

from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, Spacer, Table, TableStyle

from apps.reports.columns import display

from .base import PDFDocument
from .blocks import line_table
from .theme import ALARM, BAND, INK, RULE, styles

#: Rows past this and the table is a document nobody reads on paper. The report
#: still prints — truncated, and **saying so on the page**, because a report that
#: quietly stopped at row 2,000 is a report somebody will act on believing it was
#: the whole set. The CSV has the same cap and the same note.
MAX_PDF_ROWS = 2000

#: How a row's emphasis is drawn. A section heading inside the table, a
#: sub-total under it, and the report's own total.
_EMPHASIS_BACKGROUND = {"heading": BAND, "subtotal": BAND, "total": BAND}


def report_pdf(report, result, criteria, *, paper: str | None = None) -> bytes:
    """One registered report as a PDF. Returns the bytes.

    ``criteria`` is only read for the header block — the period, the account,
    the route — so that a page handed over on its own says what it was run for.
    Every figure on it was computed before this function was called.
    """
    paper = paper or ("a4-landscape" if report.landscape else "a4")

    pdf = PDFDocument(
        title=f"{report.title} — {result.subtitle or criteria.period_label}",
        subject=result.subtitle,
        paper=paper,
        header_title=report.title,
        header_meta=_meta(report, result, criteria),
    )
    width = pdf.width
    style = styles()

    story = []
    if result.alarm:
        story += [_alarm_box(result.alarm, available_width=width), Spacer(1, 3 * mm)]

    rows = result.rows[:MAX_PDF_ROWS]
    dropped = len(result.rows) - len(rows)

    if rows or result.totals:
        story.append(_table(report, rows, result.totals, available_width=width))
    else:
        story.append(Paragraph("Nothing on the ledger matched this report.", style["small"]))

    notes = list(result.notes)
    if dropped:
        notes.append(
            f"This page shows the first {len(rows):,} of {len(result.rows):,} rows. "
            f"{dropped:,} are not printed — narrow the period, or take the CSV."
        )
    if notes:
        story.append(Spacer(1, 3 * mm))
        for note in notes:
            story.append(Paragraph(note, style["small"]))

    pdf.build_story(story)
    return pdf.getvalue()


def _meta(report, result, criteria) -> list[tuple[str, str]]:
    """What the header block says this run was for.

    Only the filters the report actually declared: a Trial Balance header that
    said "Route: every route" would be answering a question nobody asked.
    """
    meta: list[tuple[str, str]] = []
    if "as_of" in report.filters:
        meta.append(("As of", f"{criteria.as_of:%d %b %Y}"))
    if "date_from" in report.filters or "date_to" in report.filters:
        meta.append(("Period", criteria.period_label))
    for name, label in (
        ("account", "Account"),
        ("client", "Client"),
        ("vendor", "Vendor"),
        ("route", "Route"),
        ("seller", "Seller"),
        ("item", "Item"),
        ("warehouse", "Warehouse"),
    ):
        value = getattr(criteria, name, None)
        if name in report.filters and value is not None:
            meta.append((label, str(value)))
    if criteria.include_cancelled:
        meta.append(("Including", "Cancelled"))
    return meta


def _table(report, rows, totals, *, available_width: float) -> Table:
    """The report's grid, with the totals line welded onto the bottom of it.

    The totals are a row of the same table rather than a block beside it, which
    is what keeps them under the columns they total when the table splits across
    two pages — a totals block that floated free of its columns would be a
    column of figures with a number under the wrong one.
    """
    spec = [(column.label, column.width, column.align) for column in report.columns]

    body = []
    emphasis_rows = []
    alarm_cells = []
    for index, row in enumerate(rows):
        cells = []
        for column_index, column in enumerate(report.columns):
            text = display(column, row.get(column.key))
            if row.is_cancelled and text:
                # Struck through rather than dropped: a cancelled document is
                # the correction somebody is looking for (CLAUDE.md §5), and it
                # contributes nothing to the totals either way.
                text = f"<strike>{text}</strike>"
            if column.key in row.alarm:
                alarm_cells.append((column_index, index + 1))
            cells.append(Paragraph(text, styles()["amount" if column.is_numeric else "cell"]))
        if row.emphasis:
            emphasis_rows.append((index + 1, row.emphasis))
        body.append(cells)

    total_index = None
    if totals:
        total_index = len(body) + 1
        body.append(
            [
                Paragraph(
                    f"<b>{display(column, totals.get(column.key))}</b>"
                    if column.key in totals
                    else "",
                    styles()["amount" if column.is_numeric else "cell"],
                )
                for column in report.columns
            ]
        )

    table = line_table(spec, body, available_width=available_width, zebra=not emphasis_rows)

    extra = []
    for row_index, emphasis in emphasis_rows:
        background = _EMPHASIS_BACKGROUND.get(emphasis)
        if background is not None:
            extra.append(("BACKGROUND", (0, row_index), (-1, row_index), background))
        if emphasis in {"subtotal", "total"}:
            extra.append(("LINEABOVE", (0, row_index), (-1, row_index), 0.5, RULE))
    for column_index, row_index in alarm_cells:
        extra.append(("TEXTCOLOR", (column_index, row_index), (column_index, row_index), ALARM))
    if total_index is not None:
        extra += [
            ("LINEABOVE", (0, total_index), (-1, total_index), 0.8, INK),
            ("LINEBELOW", (0, total_index), (-1, total_index), 1.2, INK),
            ("TOPPADDING", (0, total_index), (-1, total_index), 4),
            ("BOTTOMPADDING", (0, total_index), (-1, total_index), 4),
        ]
    if extra:
        table.setStyle(TableStyle(extra))
    return table


def _alarm_box(text: str, *, available_width: float) -> Table:
    """A boxed line in the alarm colour, above the table.

    What a trial balance that does not balance prints. It is at the top and it
    is not suppressible on purpose: the difference being visible is the whole
    value of the report (CLAUDE.md §6).
    """
    paragraph = Paragraph(f'<font color="#bd413f"><b>{text}</b></font>', styles()["small"])
    table = Table([[paragraph]], colWidths=[available_width], hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 0.7, ALARM),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    return table


__all__ = ["MAX_PDF_ROWS", "report_pdf"]
