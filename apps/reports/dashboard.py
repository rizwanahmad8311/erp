"""The landing screen: one dashboard, role-aware. **Reads only.**

Nothing here writes anything, and nothing here reads a total off a document
header. Every rupee on this page is aggregated by :mod:`apps.reports.ledger` or
:mod:`apps.payments.recovery`, exactly as a report is (CLAUDE.md §6) — there is
no dashboard summary table, no nightly rollup and no counter kept in step by a
signal. The dashboard is a *view* of the ledger, and the day somebody needs a
figure explained, the report behind it says the same number.

**Every card links to the report that explains it.** A number nobody can drill
into is a number nobody trusts, so :class:`Card` has no constructor path that
leaves ``url`` empty.

What each role sees
-------------------
Three questions, answered once here in :func:`audience_for` and read by
everything below, so a figure cannot be hidden on the screen and left in the
page context for a template to print by accident.

**May they see money at all?** Yes if they keep the books
(:data:`~apps.accounts.permissions.VIEW_REPORTS_FINANCIAL`) or if they collect
it (``payments.add_payment``). That is Accountant, Admin and Booker. It is
deliberately **not** Operator: an operator writes bills all day and holds
``sales.view_salesinvoice``, and the brief is that they see no financial figure
here. The rule is absolute rather than per-card — an Operator shown the route
panel's sales column and not the "sales today" card would be a screen with a
hole in it rather than a screen with a boundary.

**May they see the treasury?** Cash, bank and cheques in hand: the company's
position, so :data:`~apps.accounts.permissions.VIEW_REPORTS_FINANCIAL` and
nothing else. A route-scoped login is shown none of it however it is
permissioned, because there is no route's share of a bank balance and a figure
that cannot be scoped must not be shown to somebody who is.

**Which routes are theirs?** :func:`apps.accounts.scoping.visible_route_ids`,
the same answer every scoped screen reads. A booker's dashboard is their own
routes' shops and their own routes' money; a scoped login with no seller sees
nothing at all, which is the safe direction (see
:mod:`apps.accounts.scoping`).

Scoping is applied by **party**, not by the route on the document: a booker's
figures are the shops on their beats. That is the same definition
:func:`apps.payments.recovery.recovery_rows` scopes by and the same one the
route day sheet lists, so the dashboard and the sheet the booker carries cannot
disagree. The one panel that scopes by the document's own route is "today's
routes", which is a question about the beats rather than about the shops.

Caching
-------
This is the most-hit page in the system — every login lands on it and comes back
to it between bills — so the whole built :class:`Dashboard` is held in the
local-memory cache for ``settings.DASHBOARD_CACHE_SECONDS`` (60), keyed by user
and by day. Per user because it is role-aware and route-scoped, so one shared
entry would be one person's figures on somebody else's screen; per day so the
entry rolls over at midnight rather than showing yesterday until it expires.

:data:`CACHE_VERSION` is part of the key. Bump it whenever the shape of anything
returned here changes: the cache holds pickled dataclasses, and an entry written
by the previous shape must miss rather than come back half-populated.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from django.conf import settings
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone

from apps.accounting import chart
from apps.accounting.enums import PartyType, party_sign
from apps.accounting.models import Account
from apps.accounts.access import has_access, model_permission
from apps.accounts.permissions import VIEW_REPORTS_FINANCIAL
from apps.accounts.scoping import visible_route_ids
from apps.core.money import fmt
from apps.masters.enums import DayOfWeek
from apps.masters.models import Client, Item, Route
from apps.masters.services import fmt_qty
from apps.payments import recovery
from apps.payments.enums import AGEING_LADDER, AgeingBucket, PaymentDirection
from apps.payments.models import ChequeEvent, Payment
from apps.purchasing.models import PurchaseInvoice, PurchaseReturn
from apps.sales.models import SalesInvoice, SalesReturn

from . import ledger

#: Bump when the shape of anything cached below changes. See the module
#: docstring.
CACHE_VERSION = 1

#: How many days the sales trend covers, today included.
TREND_DAYS = 30

#: How many rows the two chase panels carry. Ten is a phone call each and a
#: morning's work; a list of two hundred is a list nobody starts.
TOP_OVERDUE = 10
LOW_STOCK_ROWS = 10
RECENT_DOCUMENTS = 10

#: A client's balance is debit-normal: positive means the shop owes us.
CLIENT_SIGN = party_sign(PartyType.CLIENT)
#: A vendor's is credit-normal: positive means we owe the supplier.
VENDOR_SIGN = party_sign(PartyType.VENDOR)

#: The ageing bands that mean "more than sixty days past due", derived from the
#: ladder rather than typed out — a band added or moved in
#: :data:`apps.payments.enums.AGEING_LADDER` is one this figure follows.
SIXTY_PLUS_BUCKETS: tuple[str, ...] = tuple(
    bucket for bucket, first_day, _last_day in AGEING_LADDER if first_day > 60
)

#: The permission that means "this person collects money", which is what makes
#: a booker's own figures theirs to see. Derived from the model for the same
#: reason :func:`apps.accounts.access.model_permission` exists.
COLLECTS_MONEY = model_permission(Payment, "add")


# ===========================================================================
# Who is looking
# ===========================================================================
@dataclass(frozen=True, slots=True)
class Audience:
    """What this login is shown, and over which routes. The one answer."""

    #: Route ids this user may see, or ``None`` meaning every route. An empty
    #: tuple means **no** routes, which is what a scoped login with no seller
    #: gets — and it must stay empty rather than becoming "all of them".
    route_ids: tuple[int, ...] | None
    #: Any rupee figure at all.
    sees_money: bool
    #: Cash, bank, cheques in hand.
    sees_treasury: bool
    #: Purchase documents exist for this person.
    sees_purchasing: bool
    #: Sales documents exist for this person.
    sees_sales: bool
    #: Receipts and payments exist for this person.
    sees_payments: bool
    #: The item master, which is what the low-stock panel is about.
    sees_stock: bool

    @property
    def is_scoped(self) -> bool:
        return self.route_ids is not None

    @property
    def single_route(self) -> int | None:
        """The one route this login walks, when there is exactly one.

        This is what makes a booker's cards drill into figures that **match**
        them. Reports are deliberately not route-scoped (CLAUDE.md: a figure
        that changed depending on who was looking at it is not a figure), so a
        card showing one beat's receivable that opened the whole company's
        ageing would be a card whose number nobody could find again. Passing the
        route as an ordinary filter is the honest fix: the report is still the
        same report for everybody, and the link simply arrives with the filter
        bar already set.

        ``None`` when the login walks several beats, because a filter bar with
        one route box cannot express two — the link then opens unfiltered, which
        is visibly a wider figure rather than a subtly wrong one.
        """
        if self.route_ids is not None and len(self.route_ids) == 1:
            return self.route_ids[0]
        return None


def audience_for(user) -> Audience:
    """The three questions in the module docstring, answered once."""
    route_ids = visible_route_ids(user)
    keeps_books = has_access(user, VIEW_REPORTS_FINANCIAL)
    collects = has_access(user, COLLECTS_MONEY)
    return Audience(
        route_ids=None if route_ids is None else tuple(route_ids),
        sees_money=keeps_books or collects,
        # A bank balance has no route share. Somebody who may only see their own
        # beats is not shown the company's position, whatever else they hold.
        sees_treasury=keeps_books and route_ids is None,
        sees_purchasing=has_access(user, model_permission(PurchaseInvoice, "view")),
        sees_sales=has_access(user, model_permission(SalesInvoice, "view")),
        sees_payments=has_access(user, model_permission(Payment, "view")),
        sees_stock=has_access(user, model_permission(Item, "view")),
    )


# ===========================================================================
# What is on the screen
# ===========================================================================
@dataclass(frozen=True, slots=True)
class Card:
    """One figure on the top row, and the report that explains it.

    ``url`` is not optional. Every card on this screen drills into the report
    that produced its number, filtered to the same day — a figure with nowhere
    to go is a figure somebody has to take on trust.

    ``sub_*`` is the second figure a card carries when the first one is not the
    whole answer: what is owed, and how much of that is badly late.

    The ``has_*`` properties exist because a Django template cannot ask whether
    something is ``None`` — ``{% if card.paisa %}`` is false for a genuine zero,
    and ``{% if card.paisa != None %}`` compares against an undefined variable
    and happens to work. A card showing a blank where the day's sales were zero
    would be a card that looked broken on a quiet morning.
    """

    key: str
    label: str
    url: str
    paisa: int | None = None
    #: A count of documents, where one is the point — cheques in the drawer.
    #: Never money: a count is a fact about documents, and money is the ledger's.
    count: int | None = None
    note: str = ""
    alarm: bool = False
    sub_label: str = ""
    sub_paisa: int | None = None
    sub_count: int | None = None
    sub_alarm: bool = False

    @property
    def has_paisa(self) -> bool:
        return self.paisa is not None

    @property
    def has_sub_paisa(self) -> bool:
        return self.sub_paisa is not None


@dataclass(frozen=True, slots=True)
class TrendPoint:
    """One day of the sales trend, and where it sits in the chart's viewBox."""

    day: dt.date
    paisa: int
    x: int
    y: int

    @property
    def label(self) -> str:
        return f"{self.day:%d %b}: {fmt(self.paisa)}"


@dataclass(frozen=True, slots=True)
class Trend:
    """The sales line, drawn as plain SVG the template only has to print.

    The geometry is computed here, in integers, for two reasons. Templates
    cannot do arithmetic worth reading, and — more to the point — there is no
    charting library in this project and there is not going to be one
    (CLAUDE.md §7 rules out a CDN, §8 rules out anything with a build step). An
    SVG polyline is forty lines of Python and no dependency at all.

    Integer arithmetic throughout, so a chart cannot introduce a second rounding
    site (CLAUDE.md §1). These are pixel coordinates, not money — but the money
    that feeds them is paisa, and keeping the whole path integral means nothing
    here can ever be mistaken for a figure.
    """

    points: tuple[TrendPoint, ...]
    peak_paisa: int
    total_paisa: int
    width: int
    height: int
    baseline_y: int
    polyline: str
    area: str
    url: str

    @property
    def has_movement(self) -> bool:
        return self.peak_paisa > 0

    @property
    def days(self) -> int:
        return len(self.points)


@dataclass(frozen=True, slots=True)
class RouteToday:
    """One beat that runs today: who walks it, and what it has done so far."""

    code: str
    name: str
    sellers: str
    invoice_count: int
    sales_paisa: int | None
    recovery_paisa: int | None
    url: str


@dataclass(frozen=True, slots=True)
class OverdueClient:
    """A shop to ring, with the number to ring it on."""

    code: str
    name: str
    phone: str
    tel: str
    route: str
    overdue_paisa: int
    outstanding_paisa: int
    bucket: str
    flagged: bool
    url: str

    @property
    def has_phone(self) -> bool:
        return bool(self.tel)


@dataclass(frozen=True, slots=True)
class LowStockItem:
    """An item at or under its reorder level, and how far under."""

    code: str
    name: str
    on_hand_pieces: int
    on_hand_display: str
    reorder_level_pieces: int
    short_pieces: int
    url: str

    @property
    def is_out(self) -> bool:
        return self.on_hand_pieces <= 0


@dataclass(frozen=True, slots=True)
class RecentDocument:
    """A document and what state it is in. Deliberately carries no amount.

    A listing, not a figure — so cancelled documents are **on** it, watermarked
    by their status chip rather than hidden (CLAUDE.md §5). It shows no money
    precisely because it is a listing: the only honest amount would come from
    the ledger, and the ledger's answer for a cancelled document is zero, which
    is true and would read as a bug on a row somebody is looking at to find out
    what was reversed.
    """

    code: str
    kind: str
    party: str
    posting_date: dt.date
    status: str
    url: str


@dataclass(frozen=True, slots=True)
class Dashboard:
    """Everything the screen renders. Plain data — no querysets, no models.

    Plain because it is pickled into the cache, and because a template handed a
    model can always reach one more relation and turn a cached page back into a
    query per row.
    """

    day: dt.date
    cards: tuple[Card, ...]
    trend: Trend | None
    routes: tuple[RouteToday, ...]
    overdue: tuple[OverdueClient, ...]
    low_stock: tuple[LowStockItem, ...]
    documents: tuple[RecentDocument, ...]
    shows_money: bool
    shows_stock: bool
    is_scoped: bool
    route_count: int

    @property
    def has_cards(self) -> bool:
        return bool(self.cards)


# ===========================================================================
# The cache
# ===========================================================================
def cache_key(user, day: dt.date) -> str:
    """Per user, per day, per shape. See the module docstring."""
    return f"dashboard:{CACHE_VERSION}:{user.pk}:{day.isoformat()}"


def dashboard_for(user, *, day: dt.date | None = None) -> Dashboard:
    """The dashboard for this login, from the cache when it is warm.

    ``day`` is for the tests and for nothing else; the screen is always today.
    """
    day = day or timezone.localdate()
    key = cache_key(user, day)

    cached = cache.get(key)
    if cached is not None:
        return cached

    built = build(user, day=day)
    cache.set(key, built, settings.DASHBOARD_CACHE_SECONDS)
    return built


def invalidate(user, *, day: dt.date | None = None) -> None:
    """Drop one login's cached dashboard. For the tests and the shell.

    Nothing in the posting path calls this, on purpose: a minute of staleness on
    a summary screen is a fair trade, and a cache invalidated from inside a
    posting transaction is I/O under SQLite's write lock (CLAUDE.md §4).
    """
    cache.delete(cache_key(user, day or timezone.localdate()))


# ===========================================================================
# Building it
# ===========================================================================
def build(user, *, day: dt.date | None = None) -> Dashboard:
    """Aggregate the whole screen. Uncached — :func:`dashboard_for` wraps this."""
    day = day or timezone.localdate()
    audience = audience_for(user)
    party_ids = _scoped_client_ids(audience)

    receivable = (
        recovery.recovery_rows(as_of=day, routes=audience.route_ids) if audience.sees_money else []
    )

    return Dashboard(
        day=day,
        cards=_cards(day, audience, party_ids, receivable),
        trend=_trend(day, audience, party_ids) if audience.sees_money else None,
        routes=_routes_today(day, audience),
        overdue=_overdue(receivable) if audience.sees_money else (),
        low_stock=_low_stock(day) if audience.sees_stock else (),
        documents=_recent_documents(audience),
        shows_money=audience.sees_money,
        shows_stock=audience.sees_stock,
        is_scoped=audience.is_scoped,
        route_count=len(audience.route_ids or ()),
    )


def _scoped_client_ids(audience: Audience) -> list[int] | None:
    """The shops this login's figures cover, or ``None`` for all of them.

    An **empty list** for a scoped user with no routes, which every ledger
    primitive here turns into an empty result. That is the whole point: the
    unsafe answer to "which shops are this login's" is "all of them".
    """
    if audience.route_ids is None:
        return None
    return list(Client.objects.filter(route_id__in=audience.route_ids).values_list("pk", flat=True))


def _day_of_week(day: dt.date) -> str:
    """``DayOfWeek`` for a date. ``weekday()`` is Monday-first, and so is the enum."""
    return DayOfWeek.values[day.weekday()]


def _report_url(slug: str, **params) -> str:
    """A registered report, filtered to the figures the card just showed.

    Built by hand rather than through :class:`~apps.reports.criteria.Criteria`
    because the bar reads GET parameters and this is the other end of the same
    contract — see :mod:`apps.reports.criteria`.
    """
    query = "&".join(f"{name}={value}" for name, value in params.items() if value is not None)
    url = reverse("reports:report", kwargs={"slug": slug})
    return f"{url}?{query}" if query else url


# ---------------------------------------------------------------------------
# The cards
# ---------------------------------------------------------------------------
def _cards(day, audience: Audience, party_ids, receivable) -> tuple[Card, ...]:
    """The top row. Empty for anybody who may not see a rupee figure."""
    if not audience.sees_money:
        return ()

    cards = [
        *_trading_cards(day, audience, party_ids),
        _receivable_card(day, audience, receivable),
    ]
    if audience.sees_treasury:
        cards += _treasury_cards(day, audience)
    return tuple(cards)


def _trading_cards(day, audience: Audience, party_ids) -> list[Card]:
    """Sales, recovery and purchases — today's movement on the ledger."""
    today = ledger.grouped_totals(
        ledger.entries(
            date_from=day,
            date_to=day,
            party_type=PartyType.CLIENT,
            party_ids=party_ids,
            voucher_types=[*ledger.SALES_VOUCHERS, *ledger.RECOVERY_VOUCHERS],
        ),
        "voucher_type",
    )

    def movement(*voucher_types: str) -> int:
        return CLIENT_SIGN * sum(
            today.get(voucher_type, ledger.Totals()).net_paisa for voucher_type in voucher_types
        )

    sales = movement("SalesInvoice")
    returns = -movement("SalesReturn")
    # Net of anything the bank sent back today. One definition of "recovery" in
    # this system and this is it — see ledger.RECOVERY_VOUCHERS.
    collected = -movement(*ledger.RECOVERY_VOUCHERS)

    cards = [
        Card(
            key="sales",
            label="Sales today",
            paisa=sales,
            note=(
                f"Credit notes of {fmt(returns)} raised today are not netted off here."
                if returns
                else "Invoices posted today, aggregated from the ledger."
            ),
            # ``route`` arrives set for a booker who walks one beat, so the
            # report opens on the same figure the card just showed. See
            # Audience.single_route.
            url=_report_url(
                "client-sales", date_from=day, date_to=day, route=audience.single_route
            ),
        ),
        Card(
            key="recovery",
            label="Recovery today",
            paisa=collected,
            note="Net of cheques the bank sent back today.",
            url=_report_url("route-performance", date_from=day, date_to=day),
        ),
    ]

    if audience.sees_purchasing:
        purchases = VENDOR_SIGN * sum(
            total.net_paisa
            for total in ledger.grouped_totals(
                ledger.entries(
                    date_from=day,
                    date_to=day,
                    party_type=PartyType.VENDOR,
                    voucher_types=["PurchaseInvoice"],
                ),
                "voucher_type",
            ).values()
        )
        cards.append(
            Card(
                key="purchases",
                label="Purchases today",
                paisa=purchases,
                note="Supplier bills posted today. Not route-scoped — a purchase has no beat.",
                url=_report_url("item-purchases", date_from=day, date_to=day),
            )
        )
    return cards


def _receivable_card(day, audience: Audience, receivable) -> Card:
    """What is owed, and how much of it is badly late.

    Both figures come out of :func:`apps.payments.recovery.recovery_rows`, which
    is the same aggregation the Accounts Receivable Ageing report prints — so the
    card and the report it links to cannot disagree.

    Outstanding is clamped at zero per shop, so a shop sitting in credit does not
    quietly cancel out what its neighbour owes.
    """
    outstanding = sum(max(row.outstanding_paisa, 0) for row in receivable)
    sixty_plus = sum(row.buckets[bucket] for row in receivable for bucket in SIXTY_PLUS_BUCKETS)
    shops = len(receivable)

    return Card(
        key="receivable",
        label="Outstanding receivable",
        paisa=outstanding,
        note=f"{shops} shop{'s' if shops != 1 else ''} with something on account.",
        url=_report_url("receivable-ageing", as_of=day, route=audience.single_route),
        sub_label="60+ days overdue",
        sub_paisa=sixty_plus,
        sub_alarm=sixty_plus > 0,
    )


def _treasury_cards(day, audience: Audience) -> list[Card]:
    """Cash, bank and the drawer full of cheques. Company position, unscoped.

    The money is the **ledger balance** of the three accounts, never a sum over
    payment headers (CLAUDE.md §6): a receipt's ``amount_paisa`` is what one
    document says, and Cheques in Hand is what the books say, which is the figure
    that has to reconcile.

    The counts beside it come from the cheque register, because "how many pieces
    of paper" is a question about documents and the ledger has never heard of a
    cheque.
    """
    totals = ledger.account_totals(date_to=day)
    accounts = {
        account.code: account
        for account in Account.objects.filter(
            code__in=[chart.CASH, chart.BANK, chart.CHEQUES_IN_HAND]
        )
    }

    def balance(code: str) -> int:
        account = accounts.get(code)
        # A chart somebody has renumbered is a chart this card reports zero for
        # rather than raising on the landing page.
        return ledger.account_balance_paisa(account, totals) if account else 0

    cash = balance(chart.CASH)
    bank = balance(chart.BANK)

    # Receipts only. A cheque we have *written* sits in Cheques Issued (2140),
    # which is a liability and not a thing in the drawer.
    pending = recovery.pending_cheques(as_of=day).filter(direction=PaymentDirection.RECEIVE)
    held = pending.count()
    week_end = day + dt.timedelta(days=6)

    return [
        Card(
            key="cash",
            label="Cash and bank",
            paisa=cash + bank,
            note=f"Cash {fmt(cash)} · Bank {fmt(bank)}. Both are ledger balances as at today.",
            url=_report_url("trial-balance", as_of=day),
        ),
        Card(
            key="cheques",
            label="Cheques in hand",
            paisa=balance(chart.CHEQUES_IN_HAND),
            count=held,
            note=(
                f"{held} cheque{'s' if held != 1 else ''} in the drawer, valued at what "
                f"account 1160 holds. A cheque is not money until the bank says so."
            ),
            url=reverse("payments:cheques"),
            sub_label="bankable within the week",
            sub_count=pending.filter(cheque_date__lte=week_end).count(),
        ),
    ]


# ---------------------------------------------------------------------------
# The sales trend
# ---------------------------------------------------------------------------
#: The chart's viewBox. Fixed, so the SVG scales to whatever column it lands in
#: without the geometry below having to know anything about the page.
CHART_WIDTH = 640
CHART_HEIGHT = 140
CHART_PAD_X = 6
CHART_PAD_TOP = 10
CHART_PAD_BOTTOM = 16


def _trend(day, audience: Audience, party_ids) -> Trend:
    """Thirty days of sales, one point a day, as an SVG path.

    A day with no sales is a point at zero rather than a gap: the line is read
    for its shape, and a chart that silently skipped the quiet days would draw a
    fortnight of nothing as a straight run of trading.
    """
    first = day - dt.timedelta(days=TREND_DAYS - 1)
    by_day = ledger.grouped_totals(
        ledger.entries(
            date_from=first,
            date_to=day,
            party_type=PartyType.CLIENT,
            party_ids=party_ids,
            voucher_types=["SalesInvoice"],
        ),
        "posting_date",
    )

    days = [first + dt.timedelta(days=offset) for offset in range(TREND_DAYS)]
    amounts = [CLIENT_SIGN * by_day.get(each, ledger.Totals()).net_paisa for each in days]

    peak = max([*amounts, 0])
    plot_width = CHART_WIDTH - 2 * CHART_PAD_X
    plot_height = CHART_HEIGHT - CHART_PAD_TOP - CHART_PAD_BOTTOM
    baseline = CHART_PAD_TOP + plot_height
    last = len(days) - 1

    points = tuple(
        TrendPoint(
            day=each,
            paisa=paisa,
            x=CHART_PAD_X + (index * plot_width) // last if last else CHART_WIDTH // 2,
            # Integer division throughout: a pixel is a pixel, and rounding lives
            # in exactly one place in this system and it is not here.
            y=baseline - ((paisa * plot_height) // peak if peak > 0 else 0),
        )
        for index, (each, paisa) in enumerate(zip(days, amounts, strict=True))
    )

    polyline = " ".join(f"{point.x},{point.y}" for point in points)
    area = f"{points[0].x},{baseline} {polyline} {points[-1].x},{baseline}" if points else ""

    return Trend(
        points=points,
        peak_paisa=peak,
        total_paisa=sum(amounts),
        width=CHART_WIDTH,
        height=CHART_HEIGHT,
        baseline_y=baseline,
        polyline=polyline,
        area=area,
        url=_report_url("client-sales", date_from=first, date_to=day),
    )


# ---------------------------------------------------------------------------
# Today's routes
# ---------------------------------------------------------------------------
def _routes_today(day, audience: Audience) -> tuple[RouteToday, ...]:
    """The beats that run today, and how far each of them has got.

    Scoped by the **route** rather than by the shop, unlike everything else on
    this screen: this panel is a question about the round, and a booker covering
    somebody else's beat is walking that beat today.

    The money is the ledger's, keyed by voucher; the route is read off the
    document, because a ledger row has never heard of one — the same split every
    report in :mod:`apps.reports.catalog.sales` makes.
    """
    routes = Route.objects.filter(is_active=True, day_of_week=_day_of_week(day))
    if audience.route_ids is not None:
        routes = routes.filter(pk__in=audience.route_ids)
    routes = list(routes.prefetch_related("route_sellers__seller").order_by("code"))
    if not routes:
        return ()

    route_ids = [route.pk for route in routes]
    invoices = dict(
        ledger.documents_in_period(
            SalesInvoice, date_from=day, date_to=day, route_id__in=route_ids
        ).values_list("pk", "route_id")
    )

    counts: dict[int, int] = {}
    for route_id in invoices.values():
        counts[route_id] = counts.get(route_id, 0) + 1

    sales: dict[int, int] = {}
    collected: dict[int, int] = {}
    if audience.sees_money:
        sales_totals = ledger.voucher_totals(
            date_from=day,
            date_to=day,
            party_type=PartyType.CLIENT,
            voucher_types=["SalesInvoice"],
        )
        for invoice_id, route_id in invoices.items():
            net = (
                CLIENT_SIGN
                * sales_totals.get(("SalesInvoice", invoice_id), ledger.Totals()).net_paisa
            )
            sales[route_id] = sales.get(route_id, 0) + net
        collected = _collected_by_route(day, route_ids)

    return tuple(
        RouteToday(
            code=route.code,
            name=route.name,
            sellers=_sellers_on(route),
            invoice_count=counts.get(route.pk, 0),
            sales_paisa=sales.get(route.pk, 0) if audience.sees_money else None,
            recovery_paisa=collected.get(route.pk, 0) if audience.sees_money else None,
            url=_report_url("route-day-sheet", route=route.pk, as_of=day),
        )
        for route in routes
    )


def _sellers_on(route) -> str:
    """Who walks this beat, primary first. ``RouteSeller`` already orders it."""
    names = [link.seller.name for link in route.route_sellers.all()]
    return ", ".join(names) if names else "—"


def _collected_by_route(day, route_ids) -> dict[int, int]:
    """What each beat took in today, net of what bounced today.

    Grouped by the route recorded **on the payment** — where the money was
    actually collected — which on a covered beat is not the shop's usual route.
    The same choice :class:`apps.payments.recovery.RouteRecovery` makes, and for
    the same reason: crediting a collection to the absent seller is how a
    performance figure becomes a reason nobody covers for anybody.

    A cheque event inherits its payment's route, and is matched on the **event's**
    own date: a cheque taken last month that came back this morning is this
    morning's problem.
    """
    owners = {
        ("Payment", pk): route_id
        for pk, route_id in ledger.documents_in_period(
            Payment,
            date_from=day,
            date_to=day,
            live=True,
            party_type=PartyType.CLIENT,
            direction=PaymentDirection.RECEIVE,
            route_id__in=route_ids,
        ).values_list("pk", "route_id")
    }
    owners.update(
        {
            ("ChequeEvent", pk): route_id
            for pk, route_id in ChequeEvent.objects.posted()
            .filter(
                posting_date=day,
                payment__party_type=PartyType.CLIENT,
                payment__direction=PaymentDirection.RECEIVE,
                payment__route_id__in=route_ids,
            )
            .values_list("pk", "payment__route_id")
        }
    )
    if not owners:
        return {}

    totals = ledger.voucher_totals(
        date_from=day,
        date_to=day,
        party_type=PartyType.CLIENT,
        voucher_types=ledger.RECOVERY_VOUCHERS,
    )
    collected: dict[int, int] = {}
    for key, route_id in owners.items():
        net = CLIENT_SIGN * totals.get(key, ledger.Totals()).net_paisa
        collected[route_id] = collected.get(route_id, 0) - net
    return collected


# ---------------------------------------------------------------------------
# Who to ring
# ---------------------------------------------------------------------------
def _overdue(receivable) -> tuple[OverdueClient, ...]:
    """The ten shops with the most money past its due date.

    Ranked by what is **overdue**, not by what is outstanding: a shop with a
    large bill that is not due yet is a good customer, and putting it at the top
    of a chase list is how a chase list stops being read.
    """
    rows = sorted(
        (row for row in receivable if row.overdue_paisa > 0),
        key=lambda row: -row.overdue_paisa,
    )[:TOP_OVERDUE]

    return tuple(
        OverdueClient(
            code=row.client.code,
            name=row.client.name,
            phone=row.client.phone or "",
            tel=_tel(row.client.phone),
            route=row.client.route.code if row.client.route_id else "—",
            overdue_paisa=row.overdue_paisa,
            outstanding_paisa=row.outstanding_paisa,
            bucket=AgeingBucket(row.worst_bucket).label,
            flagged=row.is_flagged,
            url=(
                f"{reverse('reports:report', kwargs={'slug': 'client-ledger'})}"
                f"?client={row.client.pk}"
            ),
        )
        for row in rows
    )


def _tel(phone: str | None) -> str:
    """A ``tel:`` target from whatever the operator typed in the phone box.

    Spaces, dashes and brackets are how people write a number down and are not
    part of it; a leading ``+`` is. Empty when there is nothing dialable, so the
    template renders the name rather than a link to nowhere.
    """
    digits = "".join(character for character in (phone or "") if character.isdigit())
    if not digits:
        return ""
    return f"tel:+{digits}" if (phone or "").lstrip().startswith("+") else f"tel:{digits}"


# ---------------------------------------------------------------------------
# What to buy
# ---------------------------------------------------------------------------
def _low_stock(day) -> tuple[LowStockItem, ...]:
    """Items at or below their own reorder level.

    The position is the stock ledger summed up — there is no ``current_stock``
    field to read and there must not be (CLAUDE.md §6). The level is the one new
    field on the item, and it is a *threshold*, never a balance.

    An item with a level set and no stock entry at all is on this list at zero,
    which is right: never received is a stronger version of out of stock, not an
    exemption from it.
    """
    items = list(Item.objects.filter(is_active=True, reorder_level_pieces__gt=0).order_by("code"))
    if not items:
        return ()

    positions = ledger.stock_by_item(date_to=day, item_ids=[item.pk for item in items])

    rows = []
    for item in items:
        on_hand = positions.get(item.pk, ledger.StockTotals()).qty_base
        if on_hand > item.reorder_level_pieces:
            continue
        rows.append(
            LowStockItem(
                code=item.code,
                name=item.name,
                on_hand_pieces=on_hand,
                on_hand_display=fmt_qty(item, on_hand),
                reorder_level_pieces=item.reorder_level_pieces,
                short_pieces=item.reorder_level_pieces - on_hand,
                url=_report_url("stock-balance", item=item.pk, as_of=day),
            )
        )

    # Furthest under its own line first — the buyer's order of work, and the
    # reason the level is per item rather than one figure for the whole catalogue.
    rows.sort(key=lambda row: (-row.short_pieces, row.code))
    return tuple(rows[:LOW_STOCK_ROWS])


# ---------------------------------------------------------------------------
# What just happened
# ---------------------------------------------------------------------------
def _recent_documents(audience: Audience) -> tuple[RecentDocument, ...]:
    """The last few documents of every type this login may open.

    Cancelled ones are **on** this list. It is a listing, not a figure, and a
    cancelled document is the correction somebody is looking for (CLAUDE.md §5)
    — hiding it is the opposite of an audit trail.
    """
    rows: list[RecentDocument] = []

    if audience.sees_sales:
        for model, kind in ((SalesInvoice, "Sales invoice"), (SalesReturn, "Credit note")):
            queryset = model.objects.select_related("client")
            if audience.route_ids is not None:
                queryset = queryset.filter(route_id__in=audience.route_ids)
            rows += [
                _document_row(document, kind, document.client.name)
                for document in queryset.order_by("-posting_date", "-id")[:RECENT_DOCUMENTS]
            ]

    if audience.sees_purchasing:
        for model, kind in (
            (PurchaseInvoice, "Purchase invoice"),
            (PurchaseReturn, "Purchase return"),
        ):
            rows += [
                _document_row(document, kind, document.vendor.name)
                for document in model.objects.select_related("vendor").order_by(
                    "-posting_date", "-id"
                )[:RECENT_DOCUMENTS]
            ]

    if audience.sees_payments:
        queryset = Payment.objects.all()
        if audience.route_ids is not None:
            queryset = queryset.filter(route_id__in=audience.route_ids)
        # The party is a soft (type, id) pair, so there is no select_related for
        # it — this is the bulk load that stops ten rows being ten queries.
        from apps.payments.services import attach_parties

        for payment in attach_parties(queryset.order_by("-posting_date", "-id")[:RECENT_DOCUMENTS]):
            rows.append(
                _document_row(
                    payment,
                    "Receipt" if payment.direction == PaymentDirection.RECEIVE else "Payment",
                    payment.party_name,
                )
            )

    rows.sort(key=lambda row: (row.posting_date, row.code), reverse=True)
    return tuple(rows[:RECENT_DOCUMENTS])


def _document_row(document, kind: str, party: str) -> RecentDocument:
    return RecentDocument(
        code=document.code,
        kind=kind,
        party=party,
        posting_date=document.posting_date,
        status=str(document.status),
        url=document.get_absolute_url(),
    )


__all__ = [
    "CACHE_VERSION",
    "LOW_STOCK_ROWS",
    "SIXTY_PLUS_BUCKETS",
    "TOP_OVERDUE",
    "TREND_DAYS",
    "Audience",
    "Card",
    "Dashboard",
    "LowStockItem",
    "OverdueClient",
    "RecentDocument",
    "RouteToday",
    "Trend",
    "TrendPoint",
    "audience_for",
    "build",
    "cache_key",
    "dashboard_for",
    "invalidate",
]
