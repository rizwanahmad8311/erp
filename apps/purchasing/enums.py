"""Document prefixes for the purchasing documents.

The prefix is half of a document's identity — ``PI-2026-000123`` is a purchase
invoice and nothing else, for as long as the ledger row that names it exists. It
is therefore written down once, here, and read by
:func:`apps.core.services.get_next_code`. Never build a code by hand and never
change a prefix on an installation that has posted anything under it.
"""

#: Purchase invoice: goods in, money owed to a supplier.
PURCHASE_INVOICE_PREFIX = "PI"

#: Purchase return: goods back out to that supplier, money owed reduced.
PURCHASE_RETURN_PREFIX = "PR"
