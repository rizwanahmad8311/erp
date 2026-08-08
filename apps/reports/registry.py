"""What a report *is*, and the list of the ones this system has.

A report is four things: a slug, a list of columns, the filters it wants on the
bar, and a function that turns :class:`~apps.reports.criteria.Criteria` into
rows. It is not a view, not a template and not a URL — those are shared, once,
by :mod:`apps.reports.framework`, which is what makes adding a report a matter
of writing one function rather than a view, three templates and a URL entry.

    @register
    def trial_balance() -> Report:
        return Report(slug="trial-balance", ..., build=_build)

Registration happens at import time, and the imports happen in
:meth:`apps.reports.apps.ReportsConfig.ready`. A report that is written and not
imported does not exist, which is the intended failure: it is visible
immediately as a missing entry on the index rather than as a URL that 404s.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from apps.accounts.access import has_access
from apps.accounts.masking import may_see_cost
from apps.accounts.permissions import VIEW_REPORTS

from .columns import Column, ReportRow

#: The three headings the index groups reports under, in the order the office
#: thinks about them. A report naming a group that is not here still renders —
#: it simply sorts to the end, which is a nudge and not a build failure.
GROUPS = ("Accounting", "Stock", "Sales & route")


@dataclass(frozen=True, slots=True)
class ReportResult:
    """What a report's ``build`` function returns.

    ``rows`` is the whole answer, every row of it. Pagination and any export
    limit are applied afterwards by the view, so ``totals`` is always the total
    of the *complete* set and never of the page in front of you — a totals line
    that silently meant "this page" is the single most dangerous thing a report
    can print.

    ``notes`` are printed under the table in all three formats. ``alarm`` is a
    note that is wrong rather than merely worth reading: a trial balance that
    does not balance, a statement that does not tie out. It is shown in the
    alarm colour and it is never suppressed — see
    :func:`apps.reports.catalog.accounting.trial_balance`.
    """

    rows: list[ReportRow] = field(default_factory=list)
    totals: dict[str, object] = field(default_factory=dict)
    subtitle: str = ""
    notes: tuple[str, ...] = ()
    alarm: str = ""

    @property
    def row_count(self) -> int:
        return len(self.rows)


@dataclass(frozen=True, slots=True)
class Report:
    """One report, declared once and rendered as HTML, CSV and PDF.

    ``filters`` names entries in :data:`apps.reports.criteria.FILTER_FIELDS`, in
    the order they appear on the bar. The cancelled toggle is added to every
    report and is not listed here.

    ``landscape`` is a property of the columns, not a preference: a report ten
    columns wide does not fit on A4 portrait, and finding that out at the
    printer is finding it out too late.
    """

    slug: str
    title: str
    group: str
    description: str
    columns: tuple[Column, ...]
    build: Callable[..., ReportResult]
    filters: tuple[str, ...] = ()
    landscape: bool = False
    #: The permission that opens this report. Defaults to "may open the reports
    #: section at all"; the financial statements declare
    #: :data:`~apps.accounts.permissions.VIEW_REPORTS_FINANCIAL` instead,
    #: because a stock balance is an operational question and what the owner
    #: earned is not.
    permission: str = VIEW_REPORTS
    #: Filters the report cannot run without. The screen asks for them rather
    #: than returning a confident empty table — a General Ledger with no account
    #: chosen is not "no rows", it is a question nobody has answered yet.
    requires: tuple[str, ...] = ()

    def __post_init__(self):
        missing = set(self.requires) - set(self.filters)
        if missing:
            raise ValueError(
                f"Report {self.slug!r} requires {sorted(missing)} but does not offer "
                f"{'them' if len(missing) > 1 else 'it'} on the filter bar."
            )

    @property
    def url_name(self) -> str:
        return "reports:report"

    def column(self, key: str) -> Column | None:
        return next((column for column in self.columns if column.key == key), None)

    def columns_for(self, user) -> tuple[Column, ...]:
        """The columns this user may be shown. **The masking seam.**

        Every one of the three formats goes through here — the HTML table, the
        CSV and the PDF — which is the whole design: masking applied where the
        columns are *chosen* rather than where they are *drawn* cannot be
        honoured on the screen and forgotten in the export, which is the usual
        way a cost price leaks.

        A column the user may not see is **absent**, not blank. See
        :mod:`apps.accounts.masking`.
        """
        if may_see_cost(user):
            return self.columns
        return tuple(column for column in self.columns if not column.sensitive)

    @property
    def has_sensitive_columns(self) -> bool:
        return any(column.sensitive for column in self.columns)

    def may_be_seen_by(self, user) -> bool:
        """Whether this report appears on the index and opens when clicked."""
        return has_access(user, VIEW_REPORTS, self.permission)

    def missing_requirements(self, criteria) -> list[str]:
        """Which required filters this run has not been given."""
        return [name for name in self.requires if getattr(criteria, name, None) is None]


#: Every registered report, keyed by slug, in registration order.
REPORTS: dict[str, Report] = {}


def register(report: Report) -> Report:
    """Add a report to the catalogue. A duplicate slug is a hard error.

    Two reports sharing a slug would mean one of them silently unreachable, and
    which one depended on import order — the kind of bug that is found by
    somebody asking why the Trial Balance now shows stock.
    """
    if report.slug in REPORTS:
        raise ValueError(f"A report is already registered under the slug {report.slug!r}.")
    REPORTS[report.slug] = report
    return report


def get_report(slug: str) -> Report | None:
    return REPORTS.get(slug)


def grouped(user=None) -> list[tuple[str, list[Report]]]:
    """Every report this user may open, grouped for the index.

    Filtered rather than greyed out: an index that lists the Balance Sheet to
    somebody who cannot open it is an index that tells them what they are
    missing and then refuses. A group with nothing left in it disappears too.
    """
    order = {name: index for index, name in enumerate(GROUPS)}
    groups: dict[str, list[Report]] = {}
    for report in REPORTS.values():
        if user is not None and not report.may_be_seen_by(user):
            continue
        groups.setdefault(report.group, []).append(report)
    return sorted(groups.items(), key=lambda pair: order.get(pair[0], len(GROUPS)))


def all_reports() -> Iterable[Report]:
    return REPORTS.values()


__all__ = [
    "GROUPS",
    "REPORTS",
    "Report",
    "ReportResult",
    "all_reports",
    "get_report",
    "grouped",
    "register",
]
