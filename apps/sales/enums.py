"""Document prefixes for the sales documents.

The prefix is half of a document's identity — ``SI-2026-000123`` is a sales
invoice and nothing else, for as long as the ledger row that names it exists. It
is therefore written down once, here, and read by
:func:`apps.core.services.get_next_code`. Never build a code by hand and never
change a prefix on an installation that has posted anything under it.
"""

#: Sales invoice: goods out, money owed by a client.
SALES_INVOICE_PREFIX = "SI"

#: Sales return (credit note): goods back in, money owed reduced.
SALES_RETURN_PREFIX = "SR"
