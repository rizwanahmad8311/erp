"""Forms for the sales entry screens.

The **system boundary** — the one place in the request where a human's typing
becomes a number, which is why ``Decimal`` is allowed to exist here and nowhere
downstream (CLAUDE.md §1). ``RupeeField`` is purchasing's, imported rather than
copied: there is one way to turn typed rupees into stored paisa.

Nothing here computes a total. The forms produce validated inputs; the
arithmetic is :func:`apps.masters.pricing.compute_line` and the view calls it.

The client and item fields are **hidden** primary keys fed by an autocomplete
that the operator drives from the keyboard. A ``<select>`` of four thousand
items is not usable on a counter, and a text box that guesses is worse — the
autocomplete searches on the server and the operator picks a real row.
"""

from django import forms
from django.core.exceptions import ValidationError

from apps.accounting.models import Warehouse
from apps.masters.enums import Unit
from apps.masters.models import Client, Item, Route, Seller

# One implementation of "rupees in, integer paisa out" for the whole system.
from apps.purchasing.forms import AMOUNT_INPUT_CLASS, INPUT_CLASS, RupeeField

from .models import SalesInvoice, SalesReturn


class SalesDocumentForm(forms.ModelForm):
    """The header: who, where, when, and which beat it was booked on.

    ``route`` and ``seller`` are on the form and are pre-filled from the client
    rather than hidden, because the override is a real thing an operator does —
    a booker covering someone else's beat needs to say so at entry time, not
    have it silently recorded as the client's usual.

    Deliberately does **not** include the four total fields. They are recomputed
    from the lines and there is no screen anywhere that lets somebody type one in.
    """

    class Meta:
        fields = ("client", "warehouse", "route", "seller", "posting_date", "remarks")
        widgets = {
            "posting_date": forms.DateInput(
                attrs={"type": "date", "class": INPUT_CLASS}, format="%Y-%m-%d"
            ),
            "remarks": forms.Textarea(attrs={"class": INPUT_CLASS, "rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["client"].queryset = Client.objects.filter(is_active=True)
        self.fields["warehouse"].queryset = Warehouse.objects.all()
        self.fields["route"].queryset = Route.objects.filter(is_active=True)
        self.fields["seller"].queryset = Seller.objects.filter(is_active=True)
        for name in ("client", "warehouse", "route", "seller"):
            self.fields[name].widget.attrs["class"] = INPUT_CLASS
        self.fields["route"].required = False
        self.fields["seller"].required = False


class SalesInvoiceForm(SalesDocumentForm):
    class Meta(SalesDocumentForm.Meta):
        model = SalesInvoice
        fields = (*SalesDocumentForm.Meta.fields, "due_date")
        widgets = {
            **SalesDocumentForm.Meta.widgets,
            "due_date": forms.DateInput(
                attrs={"type": "date", "class": INPUT_CLASS}, format="%Y-%m-%d"
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["due_date"].required = False
        self.fields["due_date"].help_text = "Blank takes the client's credit days."


class SalesReturnForm(SalesDocumentForm):
    class Meta(SalesDocumentForm.Meta):
        model = SalesReturn
        fields = (*SalesDocumentForm.Meta.fields, "against_invoice")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        field = self.fields["against_invoice"]
        field.required = False
        field.widget.attrs["class"] = INPUT_CLASS
        field.help_text = (
            "Naming the invoice puts the goods back into stock at what they cost when "
            "they left. Without it they come back at today's average."
        )

    def clean(self):
        """A credit note and the invoice it names must be for the same client."""
        cleaned = super().clean()
        invoice = cleaned.get("against_invoice")
        client = cleaned.get("client")
        if invoice is not None and client is not None and invoice.client_id != client.pk:
            raise ValidationError(
                f"{invoice.code} was sold to {invoice.client.name}, not to {client.name}."
            )
        return cleaned


class LineEntryForm(forms.Form):
    """One row of the entry grid, in the unit the shop buys in.

    The rate is **per ``unit_input``** — per carton when the operator picked
    CARTON. That is what is quoted over the counter, and asking somebody to
    divide it by the carton size in their head before typing is how the wrong
    number gets entered.

    ``qty_input`` is an ``IntegerField``: there is no half a carton and no half
    a piece (CLAUDE.md §2), so a fraction is rejected at the boundary rather
    than rounded somewhere later.
    """

    item = forms.ModelChoiceField(
        queryset=Item.objects.none(),
        # Hidden, and set by the autocomplete. The operator types a name; the
        # server decides which row that was.
        widget=forms.HiddenInput(attrs={"id": "id_item"}),
    )
    qty_input = forms.IntegerField(
        min_value=1,
        widget=forms.NumberInput(
            attrs={
                "class": AMOUNT_INPUT_CLASS,
                "step": "1",
                "autocomplete": "off",
                "tabindex": "20",
                "id": "id_qty_input",
            }
        ),
    )
    unit_input = forms.ChoiceField(
        choices=Unit.choices,
        initial=Unit.CARTON,
        widget=forms.Select(attrs={"class": INPUT_CLASS, "tabindex": "30", "id": "id_unit_input"}),
    )
    rate_input = RupeeField(
        required=True,
        label="Rate",
        widget=forms.TextInput(
            attrs={
                "class": AMOUNT_INPUT_CLASS,
                "inputmode": "decimal",
                "autocomplete": "off",
                "tabindex": "40",
                "id": "id_rate_input",
            }
        ),
    )
    discount = RupeeField(
        label="Discount",
        widget=forms.TextInput(
            attrs={
                "class": AMOUNT_INPUT_CLASS,
                "inputmode": "decimal",
                "autocomplete": "off",
                "tabindex": "50",
                "id": "id_discount",
            }
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["item"].queryset = Item.objects.filter(is_active=True)

    def clean(self):
        """Reject a carton entry for an item that is not sold by the carton.

        ``to_base`` would accept it — a carton of one is arithmetically fine —
        but on a data-entry screen it almost always means the wrong item was
        picked, and the amount would be a carton size out.
        """
        cleaned = super().clean()
        item, unit = cleaned.get("item"), cleaned.get("unit_input")
        if item is not None and unit == Unit.CARTON and not item.allows_carton:
            raise ValidationError(
                f"{item.name} is not sold by the carton — its carton size is 1. "
                f"Enter the quantity in pieces."
            )
        return cleaned
