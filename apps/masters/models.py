"""Items, UOM, parties, routes and sellers.

Only :class:`Item` exists so far, and it is deliberately the smallest thing the
stock ledger can point a foreign key at: a code and a name. Packing/UOM
conversions, pricing, brands, categories and reorder levels belong to this app
and arrive with it — none of them are things ``apps.accounting.StockEntry``
needs in order to be correct, and a master invented in advance of the app that
owns it is a master that gets invented wrong.

What is load-bearing today is the row's *identity*: a stock entry names an item
with a real foreign key, and ``PROTECT`` on that key means an item with movement
history can never be deleted out from under it.
"""

from django.db import models

from apps.core.models import TimeStampedModel


class Item(TimeStampedModel):
    """A thing that is bought, held and sold, counted in base units (pieces).

    Quantities against this item are always whole base units — CLAUDE.md §2.
    A carton of 12 is a UOM conversion that this app will grow; it is never
    a fractional quantity on a stock row.
    """

    code = models.CharField(
        max_length=32,
        unique=True,
        help_text="Stable identifier used on documents and in stock reports.",
    )
    name = models.CharField(max_length=200)

    class Meta:
        ordering = ["code"]
        verbose_name = "item"
        verbose_name_plural = "items"

    def __str__(self) -> str:
        return f"{self.code} — {self.name}"
