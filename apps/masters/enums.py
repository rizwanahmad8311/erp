"""Enumerations for the master data.

Two of them, and both are load-bearing rather than decorative.

:class:`Unit` is the set of units a quantity may be *typed in* or *shown in*.
Storage is always whole base units (CLAUDE.md §2) — the unit is a multiplier
applied on the way in and a divisor applied on the way out, and the only code
that applies it is :mod:`apps.masters.services`.

:class:`DayOfWeek` is what makes a route a schedule rather than a label. Its
values sort alphabetically, which is not the order a week happens in, so
:data:`DAY_ORDER` exists for anything that needs the real sequence.
"""

from django.db import models


class Unit(models.TextChoices):
    """The units a quantity may be entered or displayed in.

    ``PIECE`` is one countable thing. ``CARTON`` is ``item.carton_size`` of
    them. There is deliberately no third option: every packing level a
    distribution business actually uses — dozen, half-carton, outer — is either
    one of these two or a different item.
    """

    PIECE = "PIECE", "Piece"
    CARTON = "CARTON", "Carton"


#: Short forms for display. ``fmt_qty`` renders "3 ctn + 5 pcs" from these.
UNIT_ABBREVIATIONS = {
    Unit.PIECE: "pcs",
    Unit.CARTON: "ctn",
}


class DayOfWeek(models.TextChoices):
    """The day a route is visited.

    Nullable on :class:`~apps.masters.models.Route`: a route may be unscheduled
    (a spot run, a new area not yet on the roster), and NULL is what says so.
    """

    MON = "MON", "Monday"
    TUE = "TUE", "Tuesday"
    WED = "WED", "Wednesday"
    THU = "THU", "Thursday"
    FRI = "FRI", "Friday"
    SAT = "SAT", "Saturday"
    SUN = "SUN", "Sunday"


#: Position in the week, because ``DayOfWeek.values`` sorts FRI, MON, SAT, SUN,
#: THU, TUE, WED and no week has ever gone like that. Unscheduled sorts last.
DAY_ORDER = {day: index for index, day in enumerate(DayOfWeek.values)}


def day_order(value: str | None) -> int:
    """Sort key for a (possibly NULL) day. Unscheduled routes come last."""
    return DAY_ORDER.get(value, len(DAY_ORDER))
