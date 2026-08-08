"""The two reports that are not a document: a party statement and a day sheet.

Both **read the ledger** (CLAUDE.md §6). Not a document header, not a cached
total — the client statement is ``LedgerEntry`` rows carrying the client's party
tag, running to a closing balance that must equal
:func:`~apps.accounting.services.party_balance`, and it prints that check on the
page so anybody can see the two agree.

A cancelled document contributes both its original rows and their reversals, so
it appears on a statement twice and nets to zero. That is deliberate: the
statement is the audit trail, and a shop querying "what is this SI-2026-000123
you say I owe" needs to see the line *and* the line that took it back.
"""

from __future__ import annotations

import datetime as dt

from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, Spacer, Table, TableStyle

from apps.accounting.enums import PartyType, party_sign
from apps.accounting.models import LedgerEntry
from apps.accounting.services import party_balance
from apps.core.money import Money, fmt
from apps.payments import recovery

from .base import PDFDocument
from .blocks import line_table, money_cell, signature_block, text_cell
from .theme import BAND, RULE, styles

STATEMENT_COLUMNS = [
    ("Date", 11, "l"),
    ("Voucher", 15, "l"),
    ("Particulars", 34, "l"),
    ("Debit", 13, "r"),
    ("Credit", 13, "r"),
    ("Balance", 14, "r"),
]

DAY_SHEET_COLUMNS = [
    ("#", 4, "r"),
    ("Shop", 26, "l"),
    ("Phone", 13, "l"),
    ("Outstanding", 13, "r"),
    ("Overdue", 13, "r"),
    ("Oldest", 8, "r"),
    ("Collected", 13, "r"),
    ("", 10, "l"),
]


# ===========================================================================
# Client statement
# ===========================================================================
def client_ledger_pdf(client, date_from: dt.date, date_to: dt.date, *, paper: str = "a4") -> bytes:
    """One shop's account between two dates, with an opening and closing balance.

    The arithmetic is the only thing on this page worth being careful about, and
    it is deliberately the plainest possible:

        opening  = party_balance(as_of=date_from - 1 day)
        movement = every row tagged with this client in the window
        closing  = opening + movement

    and the closing figure is checked against ``party_balance(as_of=date_to)``
    before it is printed. If those two ever disagree the statement says so in
    the alarm colour rather than quietly showing the prettier number — a
    statement that hides a discrepancy is worse than no statement.
    """
    opening = party_balance(PartyType.CLIENT, client.pk, date_from - dt.timedelta(days=1))
    entries = (
        LedgerEntry.objects.filter(
            party_type=PartyType.CLIENT,
            party_id=client.pk,
            posting_date__gte=date_from,
            posting_date__lte=date_to,
        )
        .select_related("account")
        .order_by("posting_date", "id")
    )

    sign = party_sign(PartyType.CLIENT)
    running = opening
    rows = []
    debit_total = Money.zero()
    credit_total = Money.zero()

    for entry in entries:
        running = running + Money(sign * (entry.debit_paisa - entry.credit_paisa))
        debit_total += Money(entry.debit_paisa)
        credit_total += Money(entry.credit_paisa)
        rows.append(
            [
                text_cell(entry.posting_date.strftime("%d-%m-%y")),
                text_cell(entry.voucher_code),
                text_cell(entry.remarks or entry.account.name, small=True),
                # Blank rather than 0.00 on the side a row does not touch: a
                # column of figures with zeros down half of it is a column
                # nobody can scan.
                money_cell(entry.debit_paisa or None),
                money_cell(entry.credit_paisa or None),
                money_cell(running.paisa),
            ]
        )

    closing = running
    expected = party_balance(PartyType.CLIENT, client.pk, date_to)

    pdf = PDFDocument(
        title=f"Statement of account — {client.name}",
        subject=f"{date_from} to {date_to}",
        paper=paper,
        header_title="Statement of Account",
        header_meta=[
            ("Client", f"{client.code} — {client.name}"),
            ("Period", f"{date_from:%d %b %Y} to {date_to:%d %b %Y}"),
            ("Closing", fmt(closing.paisa)),
        ],
    )
    width = pdf.width
    style = styles()

    story = [
        _balance_strip(
            [
                ("Opening balance", opening.paisa),
                ("Debits", debit_total.paisa),
                ("Credits", credit_total.paisa),
                ("Closing balance", closing.paisa),
            ],
            available_width=width,
        ),
        Spacer(1, 4 * mm),
    ]

    if rows:
        story.append(line_table(STATEMENT_COLUMNS, rows, available_width=width))
    else:
        story.append(Paragraph("Nothing moved on this account in this period.", style["small"]))

    story.append(Spacer(1, 3 * mm))
    story.append(_tie_out_note(closing.paisa, expected.paisa))

    if client.phone:
        story.append(Spacer(1, 2 * mm))
        story.append(Paragraph(f"Contact: {client.phone}", style["small"]))

    story += signature_block(
        available_width=width, left="Confirmed by the shop", right="For the company"
    )

    pdf.build_story(story)
    return pdf.getvalue()


def _tie_out_note(printed: int, expected: int) -> Paragraph:
    """Whether the statement's closing balance matches the ledger's own answer.

    Printed either way, because the check being visible is what makes the rest
    of the page trustworthy — the same reason
    :meth:`apps.payments.recovery.ClientRecovery.ties_out` exists.
    """
    style = styles()["small"]
    if printed == expected:
        return Paragraph(f"Closing balance {fmt(printed)} agrees with the ledger.", style)
    return Paragraph(
        f'<font color="#bd413f"><b>This statement does not tie out.</b> It totals '
        f"{fmt(printed)}; the ledger says {fmt(expected)}. Do not act on this page — "
        f"report it.</font>",
        style,
    )


def _balance_strip(pairs, *, available_width: float) -> Table:
    """Four figures across the top: opening, debits, credits, closing."""
    cells = []
    for label, paisa in pairs:
        cells.append(
            [
                Paragraph(label, styles()["label"]),
                Paragraph(f"<b>{fmt(paisa)}</b>", styles()["amount"]),
            ]
        )
    table = Table(
        [[cell[0] for cell in cells], [cell[1] for cell in cells]],
        colWidths=[available_width / len(pairs)] * len(pairs),
        hAlign="LEFT",
    )
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), BAND),
                ("BOX", (0, 0), (-1, -1), 0.4, RULE),
                ("INNERGRID", (0, 0), (-1, -1), 0.25, RULE),
                ("ALIGN", (0, 1), (-1, 1), "RIGHT"),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    return table


# ===========================================================================
# Route day sheet
# ===========================================================================
def route_day_sheet_pdf(route, date: dt.date, *, paper: str = "a4") -> bytes:
    """What one van's beat owes, to carry on the round.

    The sheet the recovery workspace exists to print. Every figure comes from
    :mod:`apps.payments.recovery`, which aggregates the ledger — so what is on
    the paper is what is on the screen, and neither reads a document header.

    A shop that has ever handed over a cheque that bounced is flagged in the
    alarm colour, because that is the thing somebody needs to know **before**
    agreeing to take another cheque from them.

    The last column is left blank on purpose: it is where the person walking the
    route writes what actually happened.
    """
    rows_data = recovery.recovery_rows(as_of=date, route=route)
    day = recovery.todays_recovery(on=date, route=route)
    collected, outstanding, payment_count = recovery.day_totals(day)

    pdf = PDFDocument(
        title=f"Route day sheet — {route.code} — {date}",
        subject=f"{route.name} on {date}",
        paper=paper,
        header_title="Route Day Sheet",
        header_meta=[
            ("Route", f"{route.code} — {route.name}"),
            ("Date", date.strftime("%d %b %Y")),
            ("Shops", str(len(rows_data))),
            ("Receipts today", str(payment_count)),
        ],
    )
    width = pdf.width
    style = styles()

    story = [
        _balance_strip(
            [
                ("Outstanding", outstanding),
                ("Overdue", sum(row.overdue_paisa for row in rows_data)),
                ("Collected today", collected),
            ],
            available_width=width,
        ),
        Spacer(1, 4 * mm),
    ]

    rows = []
    for index, row in enumerate(rows_data, start=1):
        flagged = row.is_flagged
        name = row.client.name
        if flagged:
            name = f'<font color="#bd413f">{name} ⚑</font>'
        rows.append(
            [
                text_cell(index),
                Paragraph(name, style["cell"]),
                text_cell(row.client.phone or "—", small=True),
                money_cell(row.open_paisa),
                money_cell(row.overdue_paisa, alarm=bool(row.overdue_paisa)),
                text_cell(f"{row.oldest_days}d" if row.oldest_days else "—"),
                money_cell(None),  # filled in by hand on the round
                text_cell(""),
            ]
        )

    if rows:
        story.append(line_table(DAY_SHEET_COLUMNS, rows, available_width=width))
        story.append(Spacer(1, 3 * mm))
        story.append(
            Paragraph(
                "⚑ marks a shop that has handed over a cheque that bounced. "
                "The last two columns are for what is collected on the round.",
                style["small"],
            )
        )
    else:
        story.append(Paragraph("No shop on this route has anything outstanding.", style["small"]))

    story += signature_block(available_width=width, left="Collected by", right="Checked in by")

    pdf.build_story(story)
    return pdf.getvalue()


__all__ = ["DAY_SHEET_COLUMNS", "STATEMENT_COLUMNS", "client_ledger_pdf", "route_day_sheet_pdf"]
