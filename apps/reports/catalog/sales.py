"""The sales and route reports — what the van did, and who owes for it.

Four reports, and all four have the same shape underneath: the **money** comes
out of the ledger, and the **dimension** — which seller booked it, which route
it was collected on — comes off the document, because a ledger row has never
heard of a seller and never will (CLAUDE.md §6 read carefully: aggregate the
ledger, join the document only for the facts it alone holds).

The Route Day Sheet is the one that gets carried. It is a piece of paper a
booker holds while walking a beat, so its arithmetic has to be checkable on the
doorstep::

    opening + sales - returns - recovery + other = closing

Every figure in that line is aggregated from the ledger, and ``other`` is
computed as the residual so the line adds up **exactly** for every shop, every
time. If an adjustment lands on a shop's account that this sheet has no column
for, it appears there rather than making the row not add up — a day sheet whose
rows do not add up is a day sheet that gets argued with at the counter.
"""

from __future__ import annotations

import datetime as dt

from django.db.models import Count
from django.urls import reverse

from apps.accounting.enums import PartyType, party_sign
from apps.masters.models import Client, Route, Seller
from apps.payments.enums import PaymentDirection
from apps.payments.models import ChequeEvent, Payment
from apps.sales.models import SalesInvoice, SalesReturn

from .. import ledger
from ..columns import CODE, COUNT, MONEY, TEXT, Column, ReportRow, total_row
from ..registry import Report, ReportResult, register

GROUP = "Sales & route"

#: A client's balance is debit-normal: positive means the shop owes us.
CLIENT_SIGN = party_sign(PartyType.CLIENT)


def _client_ledger_url(client_id: int) -> str:
    return f"{reverse('reports:report', kwargs={'slug': 'client-ledger'})}?client={client_id}"


# ===========================================================================
# Route day sheet
# ===========================================================================
DAY_SHEET_COLUMNS = (
    Column("seq", "#", COUNT, width=4),
    Column("code", "Code", CODE, width=9),
    Column("client", "Shop", TEXT, width=24),
    Column("phone", "Phone", TEXT, width=12),
    Column("opening", "Last balance", MONEY, width=13, total=True),
    Column("sales", "Sales today", MONEY, width=12, total=True, blank_zero=True),
    Column("returns", "Returns", MONEY, width=10, total=True, blank_zero=True),
    Column("recovery", "Recovery", MONEY, width=12, total=True, blank_zero=True),
    Column("other", "Other", MONEY, width=10, total=True, blank_zero=True),
    Column("closing", "Closing", MONEY, width=13, total=True),
)


def _build_route_day_sheet(criteria) -> ReportResult:
    """One route on one day: every shop, what it owed, what it did, what it owes.

    This is the sheet the booker carries, so **every** shop on the route is on
    it — including the ones that owe nothing and did nothing today. A shop
    missing from the list is a shop that does not get visited, and a sheet that
    quietly dropped the settled ones would be a sheet that stops the round the
    week a shop pays up.

    Four queries whatever the route's size: the shops, their balance before
    today, their movement today grouped by voucher type, and their balance at
    the end of today.
    """
    route = criteria.route
    day = criteria.as_of

    clients = list(
        Client.objects.filter(route=route).select_related("seller").order_by("code", "pk")
    )
    client_ids = [client.pk for client in clients]
    if not client_ids:
        return ReportResult(
            subtitle=f"{route.code} — {route.name} · {day:%d %b %Y}",
            notes=("No shop is on this route yet.",),
        )

    shared = {"party_ids": client_ids, "include_cancelled": criteria.include_cancelled}
    opening_map = ledger.party_totals(
        PartyType.CLIENT, date_to=day - dt.timedelta(days=1), **shared
    )
    closing_map = ledger.party_totals(PartyType.CLIENT, date_to=day, **shared)
    today = ledger.grouped_totals(
        ledger.entries(date_from=day, date_to=day, party_type=PartyType.CLIENT, **shared),
        "party_id",
        "voucher_type",
    )

    def movement(client_id: int, *voucher_types: str) -> int:
        """One shop's party-signed movement from some kinds of voucher, today."""
        return CLIENT_SIGN * sum(
            today.get((client_id, voucher_type), ledger.Totals()).net_paisa
            for voucher_type in voucher_types
        )

    rows = []
    for index, client in enumerate(clients, start=1):
        opening = CLIENT_SIGN * opening_map.get(client.pk, ledger.Totals()).net_paisa
        closing = CLIENT_SIGN * closing_map.get(client.pk, ledger.Totals()).net_paisa

        sales = movement(client.pk, "SalesInvoice")
        # Both of these reduce what the shop owes, so their party-signed
        # movement is negative. Negated here so the columns read as the amounts
        # a person would say out loud: "took back 500, collected 2,000".
        returns = -movement(client.pk, "SalesReturn")
        # Net of anything the bank sent back today — see RECOVERY_VOUCHERS. A
        # booker who collected Rs 5,000 and had a Rs 6,000 cheque bounce the
        # same morning did not recover Rs 5,000.
        recovery = -movement(client.pk, *ledger.RECOVERY_VOUCHERS)
        other = closing - opening - sales + returns + recovery

        rows.append(
            ReportRow(
                values={
                    "seq": index,
                    "code": client.code,
                    "client": client.name,
                    "phone": client.phone or "—",
                    "opening": opening,
                    "sales": sales,
                    "returns": returns,
                    "recovery": recovery,
                    "other": other,
                    "closing": closing,
                },
                url=_client_ledger_url(client.pk),
                alarm=frozenset({"closing"}) if closing > 0 else frozenset(),
            )
        )

    totals = total_row(DAY_SHEET_COLUMNS, rows, label_key="client")
    ties_out = (
        totals["opening"]
        + totals["sales"]
        - totals["returns"]
        - totals["recovery"]
        + totals["other"]
        == totals["closing"]
    )

    return ReportResult(
        rows=rows,
        totals=totals,
        subtitle=f"{route.code} — {route.name} · {day:%d %b %Y} · {len(rows)} shops",
        notes=(
            "Closing = last balance + sales - returns - recovery + other. Every figure is "
            "aggregated from the ledger; 'other' is whatever else touched the account "
            "today, so each row adds up exactly.",
            "Every shop on the route is listed, including the ones that owe nothing — this is "
            "the round, not a chase list.",
        ),
        alarm=(
            ""
            if ties_out
            else "The totals on this sheet do not add up. Do not carry this page — report it."
        ),
    )


register(
    Report(
        slug="route-day-sheet",
        title="Route Day Sheet",
        group=GROUP,
        description=(
            "One route for one day: every shop, its last balance, today's invoices, "
            "today's recovery and the closing balance. This is what the booker carries."
        ),
        columns=DAY_SHEET_COLUMNS,
        filters=("route", "as_of"),
        requires=("route",),
        landscape=True,
        build=_build_route_day_sheet,
    )
)


# ===========================================================================
# Seller and route performance
# ===========================================================================
PERFORMANCE_COLUMNS = (
    Column("code", "Code", CODE, width=9),
    Column("name", "Name", TEXT, width=24),
    Column("invoices", "Invoices", COUNT, width=9, total=True),
    Column("sales", "Sales", MONEY, width=14, total=True),
    Column("returns", "Returns", MONEY, width=12, total=True, blank_zero=True),
    Column("net_sales", "Net sales", MONEY, width=14, total=True),
    Column("recovery", "Recovery", MONEY, width=14, total=True),
    Column("outstanding", "Outstanding", MONEY, width=14, total=True),
)


def _sales_by_dimension(criteria, *, invoice_field: str) -> dict:
    """``{dimension_id: {invoices, sales, returns}}`` for a period.

    The money is :func:`apps.reports.ledger.voucher_totals` filtered to the
    party side of each posting — the receivable rows, which are what the shop
    was actually billed. The *dimension* is read off the document, because that
    is where a seller and a route live.

    ``POSTED`` documents only, from
    :func:`~apps.reports.ledger.documents_in_period`: a draft has written
    nothing to any ledger, and counting one would report a bill that does not
    exist yet.
    """
    figures: dict[int | None, dict[str, int]] = {}

    for model, voucher_type, direction in (
        (SalesInvoice, "SalesInvoice", 1),
        (SalesReturn, "SalesReturn", -1),
    ):
        documents = ledger.documents_in_period(
            model,
            date_from=criteria.date_from,
            date_to=criteria.date_to,
            include_cancelled=criteria.include_cancelled,
        )
        owners = dict(documents.values_list("pk", invoice_field))
        if not owners:
            continue

        totals = ledger.voucher_totals(
            date_from=criteria.date_from,
            date_to=criteria.date_to,
            party_type=PartyType.CLIENT,
            voucher_types=[voucher_type],
            include_cancelled=criteria.include_cancelled,
        )
        for document_id, owner_id in owners.items():
            net = CLIENT_SIGN * totals.get((voucher_type, document_id), ledger.Totals()).net_paisa
            bucket = figures.setdefault(owner_id, {"invoices": 0, "sales": 0, "returns": 0})
            if direction > 0:
                bucket["invoices"] += 1
                bucket["sales"] += net
            else:
                # A credit note's party movement is negative; the column shows
                # the magnitude, and net sales subtracts it back off.
                bucket["returns"] += -net

    return figures


def _recovery_by_dimension(criteria, *, payment_field: str) -> dict:
    """``{dimension_id: recovered paisa}`` — money in, net of what bounced.

    Grouped by the field recorded **on the payment**, not by the shop's usual
    beat: on a covered route the money was collected by whoever walked it, and
    crediting it to the absent seller is how a performance report becomes a
    reason nobody covers for anybody. A cheque event inherits its payment's
    seller and route for the same reason — the cheque that came back is the one
    that was taken, whoever was walking the beat the day the bank sent it back.

    Recovery is summed over :data:`apps.reports.ledger.RECOVERY_VOUCHERS`, so a
    bounce is netted off **in the period it bounced** rather than retroactively
    unmaking a collection that really did happen in March. That is the one
    definition of "recovery" in this app, and every report here uses it.
    """
    payments = ledger.documents_in_period(
        Payment,
        date_from=criteria.date_from,
        date_to=criteria.date_to,
        include_cancelled=criteria.include_cancelled,
        party_type=PartyType.CLIENT,
        direction=PaymentDirection.RECEIVE,
    )
    owners = {("Payment", pk): owner for pk, owner in payments.values_list("pk", payment_field)}

    # Cheque events are matched on the **event's** date and not the payment's:
    # a cheque taken in March that came back in April is April's problem, and
    # the payment it hangs off may be long outside this window.
    events = ChequeEvent.objects.posted().filter(
        posting_date__gte=criteria.date_from,
        posting_date__lte=criteria.date_to,
        payment__party_type=PartyType.CLIENT,
        payment__direction=PaymentDirection.RECEIVE,
    )
    owners.update(
        {
            ("ChequeEvent", pk): owner
            for pk, owner in events.values_list("pk", f"payment__{payment_field}")
        }
    )
    if not owners:
        return {}

    totals = ledger.voucher_totals(
        date_from=criteria.date_from,
        date_to=criteria.date_to,
        party_type=PartyType.CLIENT,
        voucher_types=ledger.RECOVERY_VOUCHERS,
        include_cancelled=criteria.include_cancelled,
    )
    collected: dict[int | None, int] = {}
    for key, owner_id in owners.items():
        net = CLIENT_SIGN * totals.get(key, ledger.Totals()).net_paisa
        collected[owner_id] = collected.get(owner_id, 0) - net
    return collected


def _outstanding_by_dimension(criteria, *, client_field: str) -> dict:
    """``{dimension_id: outstanding paisa}`` as at the end of the period.

    Grouped by the **shop's** own seller or route, which is the opposite choice
    from recovery above and is deliberate: whoever collected a payment is a fact
    about that morning, but whose book the debt sits in is a fact about the
    shop. Forcing the two onto one grouping makes one of them wrong on exactly
    the days it matters — the same split
    :class:`apps.payments.recovery.RouteRecovery` makes.
    """
    balances = ledger.party_totals(PartyType.CLIENT, date_to=criteria.date_to)
    if not balances:
        return {}

    owners = dict(Client.objects.filter(pk__in=balances).values_list("pk", client_field))
    outstanding: dict[int | None, int] = {}
    for client_id, totals in balances.items():
        owner_id = owners.get(client_id)
        # Clamped at zero so a shop sitting in credit does not quietly cancel
        # out what a neighbour owes.
        outstanding[owner_id] = outstanding.get(owner_id, 0) + max(
            CLIENT_SIGN * totals.net_paisa, 0
        )
    return outstanding


def _performance(criteria, *, model, invoice_field, payment_field, client_field, what: str):
    """The shared body of the seller and route performance reports."""
    sales = _sales_by_dimension(criteria, invoice_field=invoice_field)
    recovery = _recovery_by_dimension(criteria, payment_field=payment_field)
    outstanding = _outstanding_by_dimension(criteria, client_field=client_field)

    ids = {key for key in (*sales, *recovery, *outstanding) if key is not None}
    owners = {owner.pk: owner for owner in model.objects.filter(pk__in=ids)}

    rows = []
    # ``None`` last: a counter sale with no seller on it, or a walk-in that paid
    # at the till, belongs on this report rather than silently off it.
    for owner_id in [*sorted(ids, key=lambda pk: owners[pk].code), None]:
        figures = sales.get(owner_id, {"invoices": 0, "sales": 0, "returns": 0})
        collected = recovery.get(owner_id, 0)
        owed = outstanding.get(owner_id, 0)
        if not any((figures["invoices"], figures["sales"], figures["returns"], collected, owed)):
            continue

        owner = owners.get(owner_id)
        rows.append(
            ReportRow(
                values={
                    "code": owner.code if owner else "—",
                    "name": owner.name if owner else f"No {what}",
                    "invoices": figures["invoices"],
                    "sales": figures["sales"],
                    "returns": figures["returns"],
                    "net_sales": figures["sales"] - figures["returns"],
                    "recovery": collected,
                    "outstanding": owed,
                }
            )
        )

    return ReportResult(
        rows=rows,
        totals=total_row(PERFORMANCE_COLUMNS, rows, label_key="name"),
        subtitle=criteria.period_label,
        notes=(
            f"Sales and recovery are the period's movement, aggregated from the ledger. "
            f"Outstanding is the closing balance of the shops on each {what} — a position, "
            f"not a movement, so it does not add up with the columns beside it.",
            f"Recovery is grouped by the {what} recorded on the payment (who collected it); "
            f"outstanding by the {what} on the shop (whose book the debt is in).",
            "Recovery is net of cheques the bank sent back, counted in the period they "
            "bounced. The receipt itself stays on the books — it is a true record that a "
            "cheque was taken that day.",
        ),
    )


register(
    Report(
        slug="seller-performance",
        title="Seller Performance",
        group=GROUP,
        description="Sales, recovery and outstanding per seller over a period.",
        columns=PERFORMANCE_COLUMNS,
        filters=("date_from", "date_to"),
        landscape=True,
        build=lambda criteria: _performance(
            criteria,
            model=Seller,
            invoice_field="seller",
            payment_field="collected_by",
            client_field="seller",
            what="seller",
        ),
    )
)

register(
    Report(
        slug="route-performance",
        title="Route Performance",
        group=GROUP,
        description="Sales, recovery and outstanding per route over a period.",
        columns=PERFORMANCE_COLUMNS,
        filters=("date_from", "date_to"),
        landscape=True,
        build=lambda criteria: _performance(
            criteria,
            model=Route,
            invoice_field="route",
            payment_field="route",
            client_field="route",
            what="route",
        ),
    )
)


# ===========================================================================
# Client-wise sales summary
# ===========================================================================
CLIENT_SALES_COLUMNS = (
    Column("code", "Code", CODE, width=9),
    Column("client", "Client", TEXT, width=22),
    Column("route", "Route", TEXT, width=8),
    Column("seller", "Seller", TEXT, width=11),
    Column("invoices", "Invoices", COUNT, width=8, total=True),
    Column("sales", "Sales", MONEY, width=13, total=True),
    Column("returns", "Returns", MONEY, width=11, total=True, blank_zero=True),
    Column("net_sales", "Net sales", MONEY, width=13, total=True),
    Column("recovery", "Recovery", MONEY, width=13, total=True, blank_zero=True),
    Column("outstanding", "Outstanding", MONEY, width=13, total=True),
)


def _build_client_sales(criteria) -> ReportResult:
    """What each shop bought in a period, and what it still owes at the end of it.

    Pure ledger, unlike the two reports above: a ledger row carries the party it
    was raised against, so sales per client needs no join to a document at all.
    The invoice **count** does — the ledger writes several rows per bill and
    counting distinct voucher ids is what turns those back into documents.
    """
    clients = Client.objects.select_related("route", "seller")
    if criteria.route is not None:
        clients = clients.filter(route=criteria.route)
    if criteria.seller is not None:
        clients = clients.filter(seller=criteria.seller)
    client_ids = list(clients.values_list("pk", flat=True))
    if not client_ids:
        return ReportResult(subtitle=criteria.period_label)

    shared = {
        "party_type": PartyType.CLIENT,
        "party_ids": client_ids,
        "include_cancelled": criteria.include_cancelled,
    }
    movement = ledger.grouped_totals(
        ledger.entries(date_from=criteria.date_from, date_to=criteria.date_to, **shared),
        "party_id",
        "voucher_type",
    )
    closing = ledger.party_totals(
        PartyType.CLIENT,
        date_to=criteria.date_to,
        party_ids=client_ids,
        include_cancelled=criteria.include_cancelled,
    )
    counts = dict(
        ledger.entries(
            date_from=criteria.date_from,
            date_to=criteria.date_to,
            voucher_types=["SalesInvoice"],
            **shared,
        )
        .order_by()
        .values_list("party_id")
        .annotate(n=Count("voucher_id", distinct=True))
    )

    rows = []
    for client in clients.order_by("code"):
        sales = CLIENT_SIGN * movement.get((client.pk, "SalesInvoice"), ledger.Totals()).net_paisa
        returns = -CLIENT_SIGN * movement.get((client.pk, "SalesReturn"), ledger.Totals()).net_paisa
        # Both recovery vouchers, so a bounce nets off what it took back — the
        # one definition of recovery this app has, shared with the day sheet and
        # the performance reports.
        collected = -CLIENT_SIGN * sum(
            movement.get((client.pk, voucher_type), ledger.Totals()).net_paisa
            for voucher_type in ledger.RECOVERY_VOUCHERS
        )
        owed = CLIENT_SIGN * closing.get(client.pk, ledger.Totals()).net_paisa
        if not any((sales, returns, collected, owed)):
            continue

        rows.append(
            ReportRow(
                values={
                    "code": client.code,
                    "client": client.name,
                    "route": client.route.code if client.route_id else "—",
                    "seller": client.seller.name if client.seller_id else "—",
                    "invoices": counts.get(client.pk, 0),
                    "sales": sales,
                    "returns": returns,
                    "net_sales": sales - returns,
                    "recovery": collected,
                    "outstanding": owed,
                },
                url=_client_ledger_url(client.pk),
                alarm=frozenset({"outstanding"}) if owed > 0 else frozenset(),
            )
        )

    rows.sort(key=lambda row: -int(row.get("net_sales") or 0))
    return ReportResult(
        rows=rows,
        totals=total_row(CLIENT_SALES_COLUMNS, rows, label_key="client"),
        subtitle=criteria.period_label,
        notes=(
            "Sales, returns and recovery are the period's movement on each shop's account. "
            "Outstanding is its balance at the end of the period — a position, not a movement.",
            "Every row links to that shop's statement.",
        ),
    )


register(
    Report(
        slug="client-sales",
        title="Client-wise Sales Summary",
        group=GROUP,
        description="What each shop bought and paid in a period, and what it still owes.",
        columns=CLIENT_SALES_COLUMNS,
        filters=("date_from", "date_to", "route", "seller"),
        landscape=True,
        build=_build_client_sales,
    )
)


__all__ = ["GROUP"]
