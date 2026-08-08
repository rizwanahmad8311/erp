"""
The two field types every value in this system is stored as.

Money is integer paisa in a BigIntegerField. Quantity is integer base units in
a BigIntegerField. There is no Decimal and no float anywhere in the database —
see CLAUDE.md. Use these fields rather than declaring the underlying Django
field directly, so the intent is greppable and the rule is enforced in one
place.
"""

from django.core.validators import MinValueValidator
from django.db import models


class MoneyField(models.BigIntegerField):
    """An amount in integer paisa. 1 rupee == 100 paisa.

    Signed by default: ledger rows need negatives for credits and reversals.
    Pass non_negative=True for fields that can never be below zero, such as a
    unit price on a document line.
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


class QuantityField(models.BigIntegerField):
    """A quantity in integer base units (pieces).

    Never fractional. A "carton of 12" is modelled as a UOM conversion on the
    item, not as 0.0833 of a carton.
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
