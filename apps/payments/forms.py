"""Forms for the payment screens and the recovery workspace.

The **system boundary** — the one place in the request where a human's typing
becomes a number, which is why ``Decimal`` is allowed to exist here and nowhere
downstream (CLAUDE.md §1). ``RupeeField`` is purchasing's, imported rather than
copied: there is one way to turn typed rupees into stored paisa, and a second
one would be a second rounding rule.

Nothing here computes anything. The forms produce validated inputs;
:mod:`apps.payments.services` does the arithmetic and the posting.

The party field is a **hidden** primary key fed by an autocomplete the operator
drives from the keyboard, for the same reason the sales entry screen's is: a
``<select>`` of four thousand shops is not usable on a counter.
"""

from __future__ import annotations

from django import forms
from django.core.exceptions import ValidationError

from apps.masters.models import Client, Route, Seller, Vendor
from apps.purchasing.forms import INPUT_CLASS, RupeeField

from .enums import AgeingBucket, PaymentDirection, PaymentMode


class PaymentForm(forms.Form):
    """A receipt or a payment: who, how much, in what, on which beat.

    A plain ``Form``, not a ``ModelForm``, and that is deliberate. A ModelForm
    validates by building an instance and calling its ``full_clean`` — and
    :class:`~apps.payments.models.Payment` refuses to be built half-finished:
    it has no party until this form has decided which of two fields holds one,
    and no amount until ``RupeeField`` has turned typed rupees into paisa. The
    instance is built by :func:`apps.payments.services.create_payment`, which is
    the only thing that should be building one.

    The cheque fields are on the form all the time and only *required* when the
    mode is CHEQUE, so a missing cheque number is a field error on the screen
    rather than a 500 from a posting service. The model checks it again anyway,
    and so does a CHECK constraint: a form is a convenience, not a guarantee.
    """

    direction = forms.ChoiceField(
        choices=PaymentDirection.choices,
        widget=forms.Select(attrs={"class": INPUT_CLASS}),
    )
    mode = forms.ChoiceField(
        choices=PaymentMode.choices,
        widget=forms.Select(attrs={"class": INPUT_CLASS}),
    )
    posting_date = forms.DateField(
        widget=forms.DateInput(attrs={"type": "date", "class": INPUT_CLASS}, format="%Y-%m-%d"),
        help_text="The day the money changed hands.",
    )
    amount = RupeeField(required=True, label="Amount")

    client = forms.ModelChoiceField(
        queryset=Client.objects.none(),
        required=False,
        # Hidden, and set by the autocomplete. The operator types a name; the
        # server decides which shop that was.
        widget=forms.HiddenInput(attrs={"id": "id_client"}),
    )
    vendor = forms.ModelChoiceField(
        queryset=Vendor.objects.none(),
        required=False,
        widget=forms.Select(attrs={"class": INPUT_CLASS, "id": "id_vendor"}),
        help_text="For money paid out.",
    )

    cheque_no = forms.CharField(
        required=False,
        label="Cheque no",
        widget=forms.TextInput(attrs={"class": INPUT_CLASS, "autocomplete": "off"}),
    )
    cheque_date = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date", "class": INPUT_CLASS}, format="%Y-%m-%d"),
        help_text="The date written on the cheque, not the day it was taken.",
    )
    bank_name = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={"class": INPUT_CLASS, "autocomplete": "off"}),
    )

    collected_by = forms.ModelChoiceField(
        queryset=Seller.objects.none(),
        required=False,
        empty_label="—",
        widget=forms.Select(attrs={"class": INPUT_CLASS}),
        help_text="Who physically took the money.",
    )
    route = forms.ModelChoiceField(
        queryset=Route.objects.none(),
        required=False,
        empty_label="—",
        widget=forms.Select(attrs={"class": INPUT_CLASS}),
        help_text="Blank takes the shop's own beat.",
    )
    remarks = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"class": INPUT_CLASS, "rows": 2}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["client"].queryset = Client.objects.filter(is_active=True)
        self.fields["vendor"].queryset = Vendor.objects.filter(is_active=True)
        self.fields["route"].queryset = Route.objects.filter(is_active=True)
        self.fields["collected_by"].queryset = Seller.objects.filter(is_active=True)

    def clean(self):
        """The party is one of two fields, and the cheque details follow the mode."""
        cleaned = super().clean()
        direction = cleaned.get("direction")
        client, vendor = cleaned.get("client"), cleaned.get("vendor")

        party = client if direction == PaymentDirection.RECEIVE else vendor
        if party is None:
            raise ValidationError(
                "Choose the shop money came from, or the supplier it went to."
                if direction
                else "Choose which way the money moved, and who it was with."
            )
        cleaned["party"] = party

        if cleaned.get("mode") == PaymentMode.CHEQUE:
            if not cleaned.get("cheque_no"):
                self.add_error("cheque_no", "A cheque needs its number.")
            if not cleaned.get("cheque_date"):
                self.add_error(
                    "cheque_date",
                    "A cheque needs the date written on it — on a post-dated cheque that is "
                    "the earliest it can be banked.",
                )
        else:
            # Cleared rather than rejected: switching the mode back to cash on a
            # half-filled form is a correction, not a mistake worth an error.
            cleaned["cheque_no"] = ""
            cleaned["cheque_date"] = None
            cleaned["bank_name"] = ""
        return cleaned

    def payment_fields(self) -> dict:
        """The keyword arguments :func:`apps.payments.services.create_payment` wants."""
        data = self.cleaned_data
        return {
            "party": data["party"],
            "direction": data["direction"],
            "mode": data["mode"],
            "posting_date": data["posting_date"],
            "amount_paisa": data["amount"],
            "cheque_no": data.get("cheque_no") or "",
            "cheque_date": data.get("cheque_date"),
            "bank_name": data.get("bank_name") or "",
            "collected_by": data.get("collected_by"),
            "route": data.get("route"),
            "remarks": data.get("remarks") or "",
        }


class ChequeSettlementForm(forms.Form):
    """Clearing or bouncing a cheque: one date and a note.

    The kind is not on the form — it comes from which button was pressed, so
    "cleared" and "bounced" cannot be confused by a stale radio button on a
    screen somebody left open.
    """

    posting_date = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date", "class": INPUT_CLASS}, format="%Y-%m-%d"),
        help_text="Blank takes the date written on the cheque.",
    )
    remarks = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={"class": INPUT_CLASS, "autocomplete": "off"}),
    )


class RecoveryFilterForm(forms.Form):
    """The four filters the accountant actually uses, plus the as-of date.

    A plain ``Form``, submitted by GET, so a filtered sheet is a URL somebody
    can bookmark or send to whoever is doing the chasing that morning.
    """

    route = forms.ModelChoiceField(
        queryset=Route.objects.none(),
        required=False,
        empty_label="Every route",
        widget=forms.Select(attrs={"class": INPUT_CLASS}),
    )
    seller = forms.ModelChoiceField(
        queryset=Seller.objects.none(),
        required=False,
        empty_label="Every seller",
        widget=forms.Select(attrs={"class": INPUT_CLASS}),
    )
    q = forms.CharField(
        required=False,
        label="Client",
        widget=forms.TextInput(
            attrs={
                "class": INPUT_CLASS,
                "placeholder": "Code, name or phone",
                "autocomplete": "off",
                "type": "search",
            }
        ),
    )
    bucket = forms.ChoiceField(
        required=False,
        choices=[("", "Every age"), *AgeingBucket.choices],
        widget=forms.Select(attrs={"class": INPUT_CLASS}),
    )
    as_of = forms.DateField(
        required=False,
        label="As of",
        widget=forms.DateInput(attrs={"type": "date", "class": INPUT_CLASS}, format="%Y-%m-%d"),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["route"].queryset = Route.objects.filter(is_active=True)
        self.fields["seller"].queryset = Seller.objects.filter(is_active=True)

    def criteria(self) -> dict:
        """The keyword arguments :func:`apps.payments.recovery.recovery_rows` wants.

        Falls back to an empty filter set when the form has errors, so a
        mistyped date shows the whole sheet rather than a blank page with a
        message the operator has to scroll to find.
        """
        data = self.cleaned_data if self.is_valid() else {}
        return {
            "route": data.get("route") or None,
            "seller": data.get("seller") or None,
            "query": (data.get("q") or "").strip(),
            "bucket": data.get("bucket") or "",
            "as_of": data.get("as_of") or None,
        }


class AllocationForm(forms.Form):
    """One amount against one open item, built per row.

    Constructed dynamically because the rows are the client's open invoices and
    there is no fixed set of them. Each field is named ``allocate-<type>-<id>``,
    which is the same soft ``(type, id)`` pair the allocation row stores.
    """

    FIELD_PREFIX = "allocate"

    def __init__(self, *args, open_items=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.open_items = list(open_items)
        for item in self.open_items:
            self.fields[field_name(item.voucher_type, item.voucher_id)] = RupeeField(
                required=False, label=item.voucher_code
            )

    def amounts(self) -> dict[tuple[str, int], int]:
        """``{(voucher_type, voucher_id): paisa}`` for every row with money in it."""
        allocated: dict[tuple[str, int], int] = {}
        for item in self.open_items:
            paisa = self.cleaned_data.get(field_name(item.voucher_type, item.voucher_id)) or 0
            if paisa:
                allocated[(item.voucher_type, item.voucher_id)] = paisa
        return allocated

    def field_for(self, item):
        return self[field_name(item.voucher_type, item.voucher_id)]


def field_name(voucher_type: str, voucher_id: int) -> str:
    return f"{AllocationForm.FIELD_PREFIX}-{voucher_type}-{voucher_id}"


class InlineReceiptForm(forms.Form):
    """Take money from a shop without leaving the recovery sheet.

    The whole point of the workspace: the accountant is looking at a row that
    says Rs 43,000 outstanding across four invoices, and the shop is on the
    phone. This is what turns that into a posted receipt without a page change.

    Deliberately thin — mode, amount, date, who took it. Everything else
    defaults from the client, and the allocation comes from
    :class:`AllocationForm` rendered beside it.
    """

    amount = RupeeField(required=True, label="Amount")
    mode = forms.ChoiceField(
        choices=PaymentMode.choices,
        initial=PaymentMode.CASH,
        widget=forms.Select(attrs={"class": INPUT_CLASS}),
    )
    posting_date = forms.DateField(
        widget=forms.DateInput(attrs={"type": "date", "class": INPUT_CLASS}, format="%Y-%m-%d"),
    )
    collected_by = forms.ModelChoiceField(
        queryset=Seller.objects.none(),
        required=False,
        empty_label="—",
        widget=forms.Select(attrs={"class": INPUT_CLASS}),
    )
    cheque_no = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={"class": INPUT_CLASS, "autocomplete": "off"}),
    )
    cheque_date = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date", "class": INPUT_CLASS}, format="%Y-%m-%d"),
    )
    bank_name = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={"class": INPUT_CLASS, "autocomplete": "off"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["collected_by"].queryset = Seller.objects.filter(is_active=True)

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("mode") == PaymentMode.CHEQUE:
            if not cleaned.get("cheque_no"):
                self.add_error("cheque_no", "A cheque needs its number.")
            if not cleaned.get("cheque_date"):
                self.add_error("cheque_date", "A cheque needs the date written on it.")
        else:
            cleaned["cheque_no"] = ""
            cleaned["cheque_date"] = None
            cleaned["bank_name"] = ""
        return cleaned

    def payment_fields(self) -> dict:
        """The keyword arguments :func:`apps.payments.services.create_payment` wants."""
        return {
            "mode": self.cleaned_data["mode"],
            "posting_date": self.cleaned_data["posting_date"],
            "amount_paisa": self.cleaned_data["amount"],
            "collected_by": self.cleaned_data.get("collected_by"),
            "cheque_no": self.cleaned_data.get("cheque_no") or "",
            "cheque_date": self.cleaned_data.get("cheque_date"),
            "bank_name": self.cleaned_data.get("bank_name") or "",
        }
