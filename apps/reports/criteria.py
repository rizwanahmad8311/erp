"""The filter bar, and the criteria a report is run with.

Every report on this app's index is driven by the same bar. A report declares
which of these filters it wants by name and gets exactly those, in this order,
with the same widgets, the same labels and the same defaults as every other
report. A screen that spelled "As of" differently from the one next to it would
be a screen people learn twice.

    Report(filters=("as_of", "route"))   ->   [ As of ] [ Route ] [x] Cancelled

Three things are true of every filter here.

**They are GET parameters.** A filtered report is a URL, so it can be
bookmarked, mailed to whoever is doing the chasing, or turned into the CSV of
the same figures by appending ``&format=csv``.

**A bad value never blanks the page.** An unparseable date falls back to the
default and the bar says what it used, because a report that returns nothing at
all leaves the operator guessing which of six fields they got wrong.

**"Exclude cancelled" is the default, everywhere, with the toggle on the bar.**
CLAUDE.md §5: cancelled documents are left out of *figures* and never hidden
from a *listing*. The toggle is what makes the audit view a thing somebody asks
for on purpose rather than a thing they get by accident.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, replace

from django import forms
from django.utils import timezone
from django.utils.http import urlencode

from apps.accounting.models import Account, Warehouse
from apps.core.reporting import INCLUDE_CANCELLED_PARAM
from apps.masters.models import Client, Item, Route, Seller, Vendor
from apps.purchasing.forms import INPUT_CLASS

#: How far back a period report reaches when nobody has said. The month to date
#: is the period a distribution business closes over, and it is short enough
#: that landing on a report by accident is not a table scan.
DEFAULT_PERIOD_DAYS = 30

#: What "no movement recently" means on the slow-moving report until somebody
#: changes it on the bar. Two months is a season for a fast-moving grocery line.
DEFAULT_IDLE_DAYS = 60


def _today() -> dt.date:
    """Today in the installation's timezone.

    ``timezone.localdate()`` rather than ``date.today()``: ``USE_TZ`` is on and
    ``TIME_ZONE`` is Asia/Karachi, so at 3am UTC the two disagree about what day
    it is and every default on the bar shifts by one.
    """
    return timezone.localdate()


# ===========================================================================
# What a report is run with
# ===========================================================================
@dataclass(frozen=True, slots=True)
class Criteria:
    """The validated inputs a report's ``build`` function is handed.

    Every field is present whether or not the report asked for it, so a build
    function can read ``criteria.as_of`` without checking that the bar happened
    to show that field. What the declaration in
    :class:`~apps.reports.registry.Report` controls is what the *operator* is
    offered, not what the dataclass carries.
    """

    date_from: dt.date
    date_to: dt.date
    as_of: dt.date
    include_cancelled: bool = False
    days: int = DEFAULT_IDLE_DAYS
    query: str = ""

    account: Account | None = None
    client: Client | None = None
    vendor: Vendor | None = None
    route: Route | None = None
    seller: Seller | None = None
    item: Item | None = None
    warehouse: Warehouse | None = None

    @classmethod
    def default(cls) -> Criteria:
        today = _today()
        return cls(
            date_from=today - dt.timedelta(days=DEFAULT_PERIOD_DAYS),
            date_to=today,
            as_of=today,
        )

    def with_(self, **changes) -> Criteria:
        return replace(self, **changes)

    @property
    def period_label(self) -> str:
        return f"{self.date_from:%d %b %Y} to {self.date_to:%d %b %Y}"

    @property
    def day_before(self) -> dt.date:
        """The day before the window opens — where an opening balance is taken."""
        return self.date_from - dt.timedelta(days=1)


# ===========================================================================
# The bar
# ===========================================================================
def _date_field(label: str) -> forms.Field:
    return forms.DateField(
        required=False,
        label=label,
        widget=forms.DateInput(attrs={"type": "date", "class": INPUT_CLASS}, format="%Y-%m-%d"),
    )


def _model_field(label: str, queryset, empty: str) -> forms.Field:
    """A ``<select>`` over a master.

    A select rather than the keyboard autocomplete the entry screens use: a
    report is opened a few times a day by somebody sitting down to read it, not
    a hundred times an hour by somebody who cannot look away from the counter.
    The querysets are small for the same reason they are filtered to active —
    a report on a route that was retired last year is asked for by editing the
    URL, which still works.
    """
    return forms.ModelChoiceField(
        queryset=queryset,
        required=False,
        label=label,
        empty_label=empty,
        widget=forms.Select(attrs={"class": INPUT_CLASS}),
    )


#: Every filter a report may declare, by name. The value is a zero-argument
#: factory rather than a field instance, because a Django form field is mutable
#: and stateful — one instance shared between two forms is one report's queryset
#: leaking onto another's bar.
FILTER_FIELDS = {
    "date_from": lambda: _date_field("From"),
    "date_to": lambda: _date_field("To"),
    "as_of": lambda: _date_field("As of"),
    "account": lambda: _model_field(
        "Account", Account.objects.filter(is_group=False).order_by("code"), "Every account"
    ),
    "client": lambda: _model_field("Client", Client.objects.order_by("code"), "Every client"),
    "vendor": lambda: _model_field("Vendor", Vendor.objects.order_by("code"), "Every vendor"),
    "route": lambda: _model_field(
        "Route", Route.objects.filter(is_active=True).order_by("code"), "Every route"
    ),
    "seller": lambda: _model_field(
        "Seller", Seller.objects.filter(is_active=True).order_by("code"), "Every seller"
    ),
    "item": lambda: _model_field("Item", Item.objects.order_by("code"), "Every item"),
    "warehouse": lambda: _model_field(
        "Warehouse", Warehouse.objects.order_by("code"), "Every warehouse"
    ),
    "days": lambda: forms.IntegerField(
        required=False,
        min_value=1,
        max_value=3650,
        label="Idle for",
        widget=forms.NumberInput(attrs={"class": INPUT_CLASS, "step": 1}),
    ),
    "query": lambda: forms.CharField(
        required=False,
        label="Search",
        widget=forms.TextInput(
            attrs={
                "class": INPUT_CLASS,
                "placeholder": "Code or name",
                "autocomplete": "off",
                "type": "search",
            }
        ),
    ),
}

#: Filters a report may name that are dates, for the "did they narrow it" check.
DATE_FILTERS = frozenset({"date_from", "date_to", "as_of"})


class ReportFilterForm(forms.Form):
    """The bar, built from the filters one report declared.

    Unbound fields are not merely hidden — they are never added, so a stray
    ``?client=3`` on a report that has nothing to do with clients is ignored
    rather than quietly narrowing a figure somebody is about to act on.

    The cancelled toggle is added to **every** report, always last, because
    CLAUDE.md §5 makes it a property of the system rather than of a report.
    """

    def __init__(self, report, data=None):
        super().__init__(data or None)
        self.report = report

        for name in report.filters:
            factory = FILTER_FIELDS.get(name)
            if factory is None:
                raise KeyError(
                    f"Report {report.slug!r} declares an unknown filter {name!r}. "
                    f"Add it to FILTER_FIELDS or fix the declaration."
                )
            self.fields[name] = factory()

        self.fields[INCLUDE_CANCELLED_PARAM] = forms.BooleanField(
            required=False,
            label="Include cancelled",
            help_text="Reversed documents and their mirrors. Off by default.",
            widget=forms.CheckboxInput(attrs={"class": "h-4 w-4"}),
        )

    # ------------------------------------------------------------------
    def criteria(self) -> Criteria:
        """The validated inputs, with every unanswered field defaulted.

        Falls back to the defaults field by field rather than all at once, so a
        mistyped date on a two-date bar does not also throw away the route the
        operator picked.
        """
        data = self.cleaned_data if self.is_bound and self.is_valid() else {}
        base = Criteria.default()

        date_to = data.get("date_to") or base.date_to
        date_from = data.get("date_from") or base.date_from
        # Not an error, and not a silently empty report: a period that runs
        # backwards is almost always the two fields typed the wrong way round,
        # and the honest fix is to run it the way round it was meant.
        self.period_was_swapped = date_from > date_to
        if self.period_was_swapped:
            date_from, date_to = date_to, date_from

        return Criteria(
            date_from=date_from,
            date_to=date_to,
            as_of=data.get("as_of") or base.as_of,
            include_cancelled=bool(data.get(INCLUDE_CANCELLED_PARAM)),
            days=data.get("days") or DEFAULT_IDLE_DAYS,
            query=(data.get("query") or "").strip(),
            account=data.get("account"),
            client=data.get("client"),
            vendor=data.get("vendor"),
            route=data.get("route"),
            seller=data.get("seller"),
            item=data.get("item"),
            warehouse=data.get("warehouse"),
        )

    def querystring(self, **overrides) -> str:
        """This bar's own query string, with a parameter or two changed.

        What the format links and the pager are built from, so switching to CSV
        or turning a page keeps every filter the operator set. Empty values are
        dropped so the URL stays readable.
        """
        params: dict[str, str] = {}
        for name in (*self.report.filters, INCLUDE_CANCELLED_PARAM):
            raw = (self.data.get(name) or "") if self.is_bound else ""
            if raw:
                params[name] = str(raw)
        for key, value in overrides.items():
            if value in (None, "", False):
                params.pop(key, None)
            else:
                params[key] = str(value)
        # urlencode, not string formatting: a client name in ?query= carries
        # spaces and the odd ampersand, and a hand-built query string turns the
        # second half of one into a parameter of its own.
        return urlencode(params)


__all__ = [
    "DEFAULT_IDLE_DAYS",
    "DEFAULT_PERIOD_DAYS",
    "FILTER_FIELDS",
    "Criteria",
    "ReportFilterForm",
]
