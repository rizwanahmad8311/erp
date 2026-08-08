"""Printed output. **ReportLab, and nothing else.**

Five renderers, each of which takes a saved object and returns ``bytes``::

    sales_invoice_pdf(invoice)                  a shop's bill or credit note
    purchase_invoice_pdf(invoice)               a supplier's bill, as filed
    payment_receipt_pdf(payment, layout=...)    a receipt, on a sheet or a roll
    client_ledger_pdf(client, from, to)         a statement of account
    route_day_sheet_pdf(route, date)            a beat's recovery round

Why ReportLab and not an HTML-to-PDF converter
----------------------------------------------
Because of CLAUDE.md §8. WeasyPrint needs cairo, pango and GObject; xhtml2pdf
drags in a stack of its own; wkhtmltopdf and headless Chrome are binaries
somebody has to install and keep on the machine. All four turn "``pip install
-r requirements.txt``" into a support call on a Windows PC with no internet and
nobody sitting at it. ReportLab is a pure-Python wheel — its only compiled
dependency, Pillow, ships a prebuilt Windows wheel and needs no compiler — so
the deployment story in CLAUDE.md §8 stays exactly six lines long.

The cost is that a layout here is Python, not a template, and shares nothing
with the on-screen HTML. That is the trade, and it is why there are **two**
output paths rather than one:

* **the browser's own print**, driven by ``@media print`` in
  ``static/src/css/app.css``, is the fast path for the hundred bills a day that
  go straight from the screen to the printer at the counter;
* **these renderers**, reached with ``?format=pdf``, are for the file that has
  to be emailed, archived or handed over — a real PDF with a letterhead, page
  numbers, a signature line and an amount in words.

Nothing in this package writes to the database, and nothing computes money. See
CLAUDE.md §4: PDF generation must never happen inside a posting transaction.
"""

from .documents import purchase_invoice_pdf, sales_invoice_pdf
from .ledgers import client_ledger_pdf, route_day_sheet_pdf
from .receipts import payment_receipt_pdf
from .theme import RECEIPT_LAYOUTS, receipt_layout

__all__ = [
    "RECEIPT_LAYOUTS",
    "client_ledger_pdf",
    "payment_receipt_pdf",
    "purchase_invoice_pdf",
    "receipt_layout",
    "route_day_sheet_pdf",
    "sales_invoice_pdf",
]
