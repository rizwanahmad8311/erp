"""The two invoices: what a shop gets, and what a supplier's bill looks like.

Both are the same page — the same header, the same eight-column line grid, the
same totals block, the same amount in words — because they *are* the same
document facing opposite directions, and printing them from two functions that
had drifted apart is how a purchase bill ends up with a column the sales invoice
does not have.

Layout rules, in the order they matter:

* the line columns are ``#, code, description, qty, rate, discount, tax,
  amount``, and the last five are right-aligned and set in the mono face;
* the quantity is printed the way the warehouse counts it — ``"3 ctn + 5 pcs"``,
  from :func:`apps.masters.services.fmt_qty`, never as a bare piece count;
* the total is followed by the amount in words, with lakh and crore;
* a CANCELLED document gets the diagonal watermark in the alarm colour;
* an amendment prints ``Amends: SI-2026-000123`` in the meta block.

Nothing here computes a figure. Every amount is read off the document, which is
the display convenience its own module says it is — a *printed* bill is exactly
the case CLAUDE.md §6 allows a header total to be used for.
"""

from __future__ import annotations

from reportlab.lib.units import mm
from reportlab.platypus import KeepTogether, Paragraph, Spacer

from apps.core.enums import DocumentStatus
from apps.masters.services import fmt_qty

from .base import PDFDocument
from .blocks import (
    amount_in_words_block,
    line_table,
    money_cell,
    notes_block,
    qty_cell,
    signature_block,
    status_note,
    text_cell,
    totals_block,
)
from .theme import styles

#: ``(heading, width ratio, alignment)``. The ratios are relative, so the same
#: spec lays out on A4 and A5 — see :func:`apps.reports.pdf.blocks.line_table`.
LINE_COLUMNS = [
    ("#", 3, "r"),
    ("Item code", 12, "l"),
    ("Description", 33, "l"),
    ("Qty", 13, "r"),
    ("Rate", 11, "r"),
    ("Discount", 9, "r"),
    ("Tax", 9, "r"),
    ("Amount", 12, "r"),
]

#: What the watermark says on a document that has been reversed.
CANCELLED_WATERMARK = "CANCELLED"


def _meta_pairs(document, *, party_label: str, party_name: str, extra=()) -> list[tuple[str, str]]:
    """The right-hand block under the title: code, date, party, status, chain.

    ``Amends:`` is in here rather than in a footnote because it is the first
    thing somebody comparing two bills needs to see — a shop holding both wants
    to know which one is live before it reads either.
    """
    pairs = [
        ("Document", document.code),
        ("Date", document.posting_date.strftime("%d %b %Y")),
        (party_label, party_name),
    ]
    pairs.extend(extra)
    if document.amended_from_id:
        pairs.append(("Amends", document.amended_from.code))
    pairs.append(("Status", document.get_status_display().upper()))
    return pairs


def _line_rows(document) -> list[list]:
    """One grid row per document line, formatted and nothing more."""
    rows = []
    for index, line in enumerate(document.lines.select_related("item"), start=1):
        rows.append(
            [
                text_cell(index),
                text_cell(line.item.code),
                text_cell(line.item.name),
                # The warehouse's own words. A bare "41" on a delivery note is
                # how a picker loads forty-one cartons.
                qty_cell(fmt_qty(line.item, line.qty_base)),
                money_cell(line.rate_paisa),
                money_cell(line.discount_paisa),
                money_cell(line.tax_paisa),
                money_cell(line.amount_paisa),
            ]
        )
    return rows


def _totals(document) -> list[tuple[str, int, bool]]:
    pairs = [("Subtotal", document.subtotal_paisa, False)]
    if document.discount_paisa:
        pairs.append(("Discount", -document.discount_paisa, False))
    if document.tax_paisa:
        pairs.append(("Sales tax", document.tax_paisa, False))
    pairs.append(("Total", document.total_paisa, True))
    return pairs


def _build_invoice(
    document,
    *,
    title: str,
    party_label: str,
    party_name: str,
    extra_meta=(),
    signature_left: str = "",
    signature_right: str = "Authorised signature",
    paper: str = "a4",
) -> bytes:
    """The shared body of both invoice renderers. Never called directly."""
    pdf = PDFDocument(
        title=f"{title} {document.code}",
        subject=f"{title} for {party_name}",
        paper=paper,
        watermark=CANCELLED_WATERMARK if document.status == DocumentStatus.CANCELLED else "",
        header_title=title,
        header_meta=_meta_pairs(
            document, party_label=party_label, party_name=party_name, extra=extra_meta
        ),
    )
    width = pdf.width
    style = styles()

    story = [*status_note(document)]

    rows = _line_rows(document)
    if rows:
        story.append(line_table(LINE_COLUMNS, rows, available_width=width))
    else:
        story.append(Paragraph("This document has no lines.", style["small"]))

    story.append(Spacer(1, 5 * mm))
    # Kept together so the total, the words under it and the signature never
    # arrive on a page of their own with the lines left behind.
    story.append(
        KeepTogether(
            [
                totals_block(_totals(document), available_width=width * 0.45),
                Spacer(1, 4 * mm),
                amount_in_words_block(document.total_paisa, available_width=width),
            ]
        )
    )

    story += notes_block(document.remarks, available_width=width, heading="Remarks")
    story += notes_block(pdf.profile.invoice_terms, available_width=width, heading="Terms")
    story += signature_block(available_width=width, left=signature_left, right=signature_right)

    pdf.build_story(story)
    return pdf.getvalue()


# ===========================================================================
# Sales
# ===========================================================================
def sales_invoice_pdf(invoice, *, paper: str = "a4") -> bytes:
    """A sales invoice or a credit note, as the shop receives it.

    The extra meta pairs are the ones only a sale has: the route it goes out on,
    who booked it, and when it falls due. A shop chasing a delivery asks about
    the route; a shop querying a bill asks about the due date.
    """
    from apps.sales.models import SalesReturn

    is_return = isinstance(invoice, SalesReturn)
    extra = []
    if invoice.route_id:
        extra.append(("Route", invoice.route.code))
    if invoice.seller_id:
        extra.append(("Booked by", invoice.seller.name))
    if not is_return and getattr(invoice, "due_date", None):
        extra.append(("Due", invoice.due_date.strftime("%d %b %Y")))
    if is_return and invoice.against_invoice_id:
        extra.append(("Against", invoice.against_invoice.code))

    return _build_invoice(
        invoice,
        title="Credit Note" if is_return else "Sales Invoice",
        party_label="Client",
        party_name=invoice.client.name,
        extra_meta=extra,
        signature_left="Received the goods in good order",
        paper=paper,
    )


# ===========================================================================
# Purchasing
# ===========================================================================
def purchase_invoice_pdf(invoice, *, paper: str = "a4") -> bytes:
    """A purchase invoice or a purchase return, as it is filed.

    Not a document anybody sends: it is our own record of a supplier's bill, and
    the pair that matters on it is the supplier's own document number and date —
    that is what a query to the supplier is made against, not our code.
    """
    from apps.purchasing.models import PurchaseReturn

    is_return = isinstance(invoice, PurchaseReturn)
    extra = []
    if invoice.vendor_bill_no:
        label = "Their credit note" if is_return else "Their bill no."
        extra.append((label, invoice.vendor_bill_no))
    if invoice.vendor_bill_date:
        extra.append(("Their date", invoice.vendor_bill_date.strftime("%d %b %Y")))
    extra.append(("Warehouse", invoice.warehouse.code))

    return _build_invoice(
        invoice,
        title="Purchase Return" if is_return else "Purchase Invoice",
        party_label="Supplier",
        party_name=invoice.vendor.name,
        extra_meta=extra,
        signature_left="Checked and received",
        signature_right="Approved",
        paper=paper,
    )


__all__ = ["CANCELLED_WATERMARK", "LINE_COLUMNS", "purchase_invoice_pdf", "sales_invoice_pdf"]
