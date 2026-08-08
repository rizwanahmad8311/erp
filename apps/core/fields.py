"""
The two field types every value in this system is stored as.

Money is integer paisa. Quantity is integer base units (pieces). There is no
Decimal and no float in the database — see CLAUDE.md §1 and §2. Use these
fields rather than the underlying Django field so the intent is greppable and
the rule is enforced in one place.

Both fields store and return plain ``int``. They deliberately do **not** convert
to :class:`~apps.core.money.Money` on the way out: a field that returns a custom
object breaks aggregates, ``F()`` expressions, ``values_list()`` and the admin in
ways that are tedious to work around. Wrap in ``Money`` inside the service that
does the arithmetic, and write ``.paisa`` back.
"""

from django.core.validators import MinValueValidator
from django.db import models


class MoneyField(models.BigIntegerField):
    """An amount in integer paisa. 1 rupee == 100 paisa.

    Signed by default: ledger rows need negatives for credits and for the
    reversing entries a cancellation writes. Pass ``non_negative=True`` for
    fields that genuinely cannot go below zero, such as a unit price.

    64-bit because paisa are two decimal orders larger than rupees, and a
    32-bit column would top out around Rs 21 million on a single row.
    """

    description = "Monetary amount stored as integer paisa"

    def __init__(self, *args, non_negative=False, **kwargs):
        self.non_negative = non_negative
        kwargs.setdefault("default", 0)
        super().__init__(*args, **kwargs)
        if non_negative:
            self.validators.append(MinValueValidator(0))

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()
        if self.non_negative:
            kwargs["non_negative"] = True
        return name, path, args, kwargs


class QuantityField(models.IntegerField):
    """A quantity in integer base units (pieces).

    Never fractional. A "carton of 12" is a UOM conversion on the item, not
    0.0833 of a carton — see CLAUDE.md §2.

    32-bit is deliberate and is the difference from :class:`MoneyField`: piece
    counts for a distribution business stay far below two billion, and the
    narrower column makes an accidental paisa value assigned to a quantity field
    much more likely to fail loudly.
    """

    description = "Quantity stored as integer base units"

    def __init__(self, *args, non_negative=False, **kwargs):
        self.non_negative = non_negative
        kwargs.setdefault("default", 0)
        super().__init__(*args, **kwargs)
        if non_negative:
            self.validators.append(MinValueValidator(0))

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()
        if self.non_negative:
            kwargs["non_negative"] = True
        return name, path, args, kwargs
