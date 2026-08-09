"""Forms for the purchase entry screens.

This is the **system boundary** — the one place in the whole request where a
human's typing becomes a number. That is why ``Decimal`` is allowed to exist
here and nowhere downstream (CLAUDE.md §1): :class:`RupeeField` takes the string
the operator typed, hands it to :func:`apps.core.money.to_paisa`, and everything
past this point is integer paisa.

Nothing here computes a total. The forms produce validated inputs; the
arithmetic is :func:`apps.purchasing.services.compute_line`, and the view calls
it. A form that added up its own lines would be a second implementation of the
rounding rule, which is exactly one more than there should be.
"""

from django import forms
from django.core.exceptions import ValidationError

from apps.accounting.models import Warehouse
from apps.core.exceptions import MoneyError
from apps.core.money import fmt, to_paisa
from apps.masters.enums import Unit
from apps.masters.models import Item, Vendor

from .models import PurchaseInvoice, PurchaseReturn

#: Shared by every input on these screens. Kept in one place so the entry grid
#: does not drift row by row.
#:
#: ``.field`` is the component class in static/src/css/app.css: 32px high, 3px
#: radius, signal-coloured focus ring — the same height as a table row, so an
#: input sitting in the grid does not shift the rows around it by a pixel.
INPUT_CLASS = "field"
AMOUNT_INPUT_CLASS = f"{INPUT_CLASS} amount"


class RupeeField(forms.CharField):
    """Rupees in, integer paisa out.

    A ``CharField`` rather than a ``DecimalField`` on purpose. Django's
    ``DecimalField`` would hand back a ``Decimal`` that then has to be converted
    anyway, and the conversion is the part that must not be reinvented — commas,
    blanks and a refusal to accept a float all live in ``to_paisa``.
    """

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("required", False)
        kwargs.setdefault("strip", True)
        widget_class = kwargs.pop("widget_class", AMOUNT_INPUT_CLASS)
        kwargs.setdefault(
            "widget",
            forms.TextInput(
                attrs={"class": widget_class, "inputmode": "decimal", "autocomplete": "off"}
            ),
        )
        super().__init__(*args, **kwargs)

    def clean(self, value) -> int:
        value = super().clean(value)
        if value in (None, ""):
            if self.required:
                raise ValidationError("Enter an amount in rupees.")
            return 0
        try:
            return to_paisa(value)
        except MoneyError as exc:
            raise ValidationError(str(exc)) from exc

    def prepare_value(self, value):
        """Re-show a stored paisa value as the rupees the operator would type."""
        if isinstance(value, int) and not isinstance(value, bool):
            return fmt(value, thousands=False)
        return value


class PurchaseDocumentForm(forms.ModelForm):
    """The header: who, where, when, and their bill number.

    Deliberately does **not** include the four total fields. They are recomputed
    from the lines by ``services.recalculate_totals`` and there is no screen
    anywhere that lets somebody type one in.
    """

    class Meta:
        fields = (
            "vendor",
            "warehouse",
            "posting_date",
            "vendor_bill_no",
            "vendor_bill_date",
            "remarks",
        )
        widgets = {
            "posting_date": forms.DateInput(
                attrs={"type": "date", "class": INPUT_CLASS}, format="%Y-%m-%d"
            ),
            "vendor_bill_date": forms.DateInput(
                attrs={"type": "date", "class": INPUT_CLASS}, format="%Y-%m-%d"
            ),
            "vendor_bill_no": forms.TextInput(attrs={"class": INPUT_CLASS, "autocomplete": "off"}),
            "remarks": forms.Textarea(attrs={"class": INPUT_CLASS, "rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["vendor"].queryset = Vendor.objects.filter(is_active=True)
        self.fields["warehouse"].queryset = Warehouse.objects.all()
        for name in ("vendor", "warehouse"):
            self.fields[name].widget.attrs["class"] = INPUT_CLASS
        # The first field on the screen, so the caret is already there.
        self.fields["vendor"].widget.attrs["autofocus"] = "autofocus"


class PurchaseInvoiceForm(PurchaseDocumentForm):
    class Meta(PurchaseDocumentForm.Meta):
        model = PurchaseInvoice


class PurchaseReturnForm(PurchaseDocumentForm):
    class Meta(PurchaseDocumentForm.Meta):
        model = PurchaseReturn


class LineEntryForm(forms.Form):
    """One row of the entry grid, in the unit the supplier bills in.

    The rate is **per ``unit_input``** — per carton when the operator picked
    CARTON. That is what is printed on the bill, and asking somebody to divide
    it by the carton size in their head before typing is how the wrong number
    gets entered.

    ``qty_input`` is an ``IntegerField``: there is no half a carton and no half
    a piece (CLAUDE.md §2), so a fraction is rejected at the boundary rather
    than rounded somewhere later.
    """

    item = forms.ModelChoiceField(
        queryset=Item.objects.none(),
        widget=forms.Select(attrs={"class": INPUT_CLASS}),
    )
    qty_input = forms.IntegerField(
        min_value=1,
        widget=forms.NumberInput(
            attrs={"class": AMOUNT_INPUT_CLASS, "step": "1", "autocomplete": "off"}
        ),
    )
    unit_input = forms.ChoiceField(
        choices=Unit.choices,
        initial=Unit.CARTON,
        widget=forms.Select(attrs={"class": INPUT_CLASS}),
    )
    rate_input = RupeeField(required=True, label="Rate")
    discount = RupeeField(label="Discount")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["item"].queryset = Item.objects.filter(is_active=True).select_related(
            "category"
        )

    def clean(self):
        """Reject a carton entry for an item that is not sold by the carton.

        ``to_base`` would accept it — a carton of one is arithmetically fine —
        but on a data-entry screen it almost always means the operator picked
        the wrong item, and the amount would be a carton size out.
        """
        cleaned = super().clean()
        item, unit = cleaned.get("item"), cleaned.get("unit_input")
        if item is not None and unit == Unit.CARTON and not item.allows_carton:
            raise ValidationError(
                f"{item.name} is not sold by the carton — its carton size is 1. "
                f"Enter the quantity in pieces."
            )
        return cleaned
