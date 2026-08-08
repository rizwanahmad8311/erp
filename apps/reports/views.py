"""The two printed reports that are not a document's own screen.

A client statement and a route day sheet are pieces of paper somebody carries,
not screens somebody sits at — so unlike an invoice, which has an entry screen
with ``?format=pdf`` bolted on, these are PDF-only routes.

Both read the ledger through :mod:`apps.reports.pdf.ledgers`. Neither writes
anything, and neither runs inside a transaction (CLAUDE.md §4).
"""

from __future__ import annotations

import datetime as dt

from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_GET

from apps.masters.models import Client, Route

from .pdf import client_ledger_pdf, route_day_sheet_pdf
from .responses import pdf_filename, pdf_response, wants_download

#: How far back a statement reaches when no start date is given. A month is the
#: period a shop is reconciled over, and asking for "everything since go-live"
#: by accident is a slow query and a fifty-page fax.
DEFAULT_STATEMENT_DAYS = 30


def _date(request, name: str, default: dt.date) -> dt.date:
    """A ``YYYY-MM-DD`` query parameter, or a refusal naming the parameter.

    A 404 rather than a silent fallback: a statement covering a period nobody
    asked for is a statement somebody will act on.
    """
    raw = (request.GET.get(name) or "").strip()
    if not raw:
        return default
    try:
        return dt.date.fromisoformat(raw)
    except ValueError:
        raise Http404(f"{name} must be a date as YYYY-MM-DD, got {raw!r}.") from None


@login_required
@require_GET
def client_ledger(request, pk: int):
    """One shop's statement of account between two dates.

    /reports/clients/12/ledger/?from=2026-04-01&to=2026-04-30
    """
    client = get_object_or_404(Client.objects.select_related("route", "seller"), pk=pk)
    today = timezone.localdate()
    date_to = _date(request, "to", today)
    date_from = _date(request, "from", date_to - dt.timedelta(days=DEFAULT_STATEMENT_DAYS))

    if date_from > date_to:
        raise Http404(f"The period runs backwards: {date_from} is after {date_to}.")

    return pdf_response(
        client_ledger_pdf(client, date_from, date_to, paper=request.GET.get("paper") or "a4"),
        pdf_filename("statement", client.code, str(date_from), str(date_to)),
        download=wants_download(request),
    )


@login_required
@require_GET
def route_day_sheet(request, pk: int):
    """One route's recovery round for a day.

    /reports/routes/3/day-sheet/?date=2026-06-30
    """
    route = get_object_or_404(Route, pk=pk)
    date = _date(request, "date", timezone.localdate())

    return pdf_response(
        route_day_sheet_pdf(route, date, paper=request.GET.get("paper") or "a4"),
        pdf_filename("day-sheet", route.code, str(date)),
        download=wants_download(request),
    )
