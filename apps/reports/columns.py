"""A report's columns, declared once and rendered three ways.

Every report in this app is a list of :class:`Column` and a function that
returns :class:`ReportRow` objects. The HTML table, the CSV file and the
ReportLab PDF all read that same list — so a column added to the Trial Balance
appears in all three, in the same order, aligned the same way, and there is no
second place to forget.

Two rules follow the columns everywhere they go.

**A numeric column is right-aligned and set in the mono face.** On screen that
is ``.amount`` (``font-variant-numeric: tabular-nums``); on paper it is the
mono style in :func:`apps.reports.pdf.theme.styles`. A column of figures that
does not line up digit for digit is a column nobody can add by eye, which is the
only thing a printed report is for.

**Nothing here computes money.** A cell arrives holding integer paisa or an
integer quantity, and this module turns it into a string. The arithmetic
happened in the report's ``build`` function, which got its figures from the
ledger (CLAUDE.md §6).

Display and export deliberately differ. ``1,234.50`` is what a human reads;
``1234.50`` is what a spreadsheet parses, and a comma inside a CSV number is a
column split in two. Same for dates: ``08 Aug 2026`` on the page, ``2026-08-08``
in the file, because a file that crosses a border must not depend on whether the
reader's locale puts the day or the month first.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from apps.core.money import fmt, to_rupees

# ---------------------------------------------------------------------------
# The kinds of value a cell can hold
# ---------------------------------------------------------------------------
#: Free text — a name, an account title, a remark.
TEXT = "text"
#: An identifier: a document code, an item code, an account number. Set in mono
#: on screen so a column of codes lines up, but not right-aligned.
CODE = "code"
#: A ``datetime.date``.
DATE = "date"
#: Integer **paisa** (CLAUDE.md §1). Never rupees, never a float.
MONEY = "money"
#: A quantity. Either integer base units, or a string already formatted by
#: :func:`apps.masters.services.fmt_qty` as ``"3 ctn + 5 pcs"``.
QTY = "qty"
#: A plain integer that is neither money nor a quantity — a count of documents.
COUNT = "count"
#: Integer basis points, rendered as a percentage. Integer arithmetic like every
#: other percentage in this system; display only.
PERCENT = "percent"

KINDS = frozenset({TEXT, CODE, DATE, MONEY, QTY, COUNT, PERCENT})

#: Kinds that are right-aligned and set in the mono face.
NUMERIC_KINDS = frozenset({MONEY, QTY, COUNT, PERCENT})

#: Kinds a totals row can sum. A date has no total and neither does a name; a
#: percentage has one only in the sense of being recomputed, which the report
#: does itself rather than by adding a column up.
TOTALLABLE_KINDS = frozenset({MONEY, COUNT})


@dataclass(frozen=True, slots=True)
class Column:
    """One column of a report, in every format it will ever be rendered in.

    ``key`` is what the row's dict is keyed by. ``width`` is a *ratio*, not a
    measurement: the PDF scales the ratios to whatever the frame actually is, so
    the same declaration lays out on A4 portrait and A4 landscape without a
    second set of numbers to keep in step.

    ``total=True`` puts the column into the report's totals row. It is refused
    on a kind that cannot be added up, because a "total" under a column of dates
    is worse than no total at all.
    """

    key: str
    label: str
    kind: str = TEXT
    width: int = 10
    #: Whether this column is summed into the totals row.
    total: bool = False
    #: Whether the cell links to the document the row references (``row.url``).
    #: Only ever set on the column carrying the document code — see
    #: :class:`ReportRow`.
    link: bool = False
    #: Render 0 as an empty cell. A ledger statement has a debit column and a
    #: credit column and every row touches exactly one of them; a column with
    #: ``0.00`` down half of it is a column nobody can scan.
    blank_zero: bool = False
    #: This column shows what something **cost** — COGS, margin, a valuation
    #: rate, a purchase rate. A user without ``masters.view_cost_price`` does
    #: not get the column at all, in any of the three formats. Dropped rather
    #: than blanked: a masked column still says a cost exists and roughly where,
    #: and an empty column in a CSV invites somebody to ask why. See
    #: :meth:`apps.reports.registry.Report.columns_for`.
    sensitive: bool = False

    def __post_init__(self):
        if self.kind not in KINDS:
            raise ValueError(f"Unknown column kind {self.kind!r}; expected one of {sorted(KINDS)}.")
        if self.total and self.kind not in TOTALLABLE_KINDS:
            raise ValueError(
                f"Column {self.key!r} is {self.kind} and cannot be totalled. "
                f"Only {sorted(TOTALLABLE_KINDS)} add up to something meaningful."
            )

    @property
    def is_numeric(self) -> bool:
        return self.kind in NUMERIC_KINDS

    @property
    def align(self) -> str:
        """``"l"`` or ``"r"`` — the alignment the PDF's ``line_table`` wants."""
        return "r" if self.is_numeric else "l"

    @property
    def css_class(self) -> str:
        """The classes the HTML cell carries.

        ``amount`` is the screen half of "numeric columns are tabular" — it is
        defined in ``static/src/css/app.css`` and is the same class the invoice
        line grid uses.
        """
        if self.is_numeric:
            return "amount"
        if self.kind == CODE:
            return "font-mono"
        return ""


@dataclass(frozen=True, slots=True)
class ReportRow:
    """One line of a report: its cells, and what it points at.

    ``url`` is the document this row came from, and it is what makes
    "every report row that references a document links to that document" a
    property of the framework rather than of eighteen separate templates. A row
    that summarises many documents — an ageing bucket, an item total — carries
    no URL and simply renders as text.

    ``emphasis`` is how a report says a row is not an ordinary one:

        ``""``          an ordinary row
        ``"opening"``   a brought-forward balance, above the movement
        ``"heading"``   a section title inside the table (Assets, Income)
        ``"subtotal"``  a section total
        ``"total"``     the report's own total, if it sits in the body

    ``alarm`` names the cells to print in the alarm colour — overdue money, an
    out-of-balance difference. The same colour on screen and on paper, because
    the two are read by the same person for the same reason.
    """

    values: dict[str, object]
    url: str = ""
    status: str = ""
    emphasis: str = ""
    alarm: frozenset[str] = field(default_factory=frozenset)

    def get(self, key: str):
        return self.values.get(key)

    @property
    def is_cancelled(self) -> bool:
        """Whether this row's document was reversed.

        Cancelled rows are shown — never hidden, never deleted (CLAUDE.md §5) —
        struck through, so nobody adds them up by eye. What keeps them out of
        the *figures* is the ledger, where their entries and the reversals net
        to zero.
        """
        return self.status == "CANCELLED"

    @property
    def is_emphasised(self) -> bool:
        return bool(self.emphasis)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def display(column: Column, value) -> str:
    """The cell as a human reads it — on screen and on paper.

    Both the HTML table and the PDF go through here, which is the point: a
    figure that reads ``1,234.50`` in the browser and ``1234.5`` on the printout
    is a figure somebody will eventually reconcile the hard way.
    """
    if value is None or value == "":
        return ""

    if column.kind == MONEY:
        paisa = int(value)
        if column.blank_zero and paisa == 0:
            return ""
        return fmt(paisa)

    if column.kind == QTY:
        # Already-formatted quantities ("3 ctn + 5 pcs") pass straight through;
        # a bare int is base units and gets thousands separators.
        if isinstance(value, str):
            return value
        quantity = int(value)
        if column.blank_zero and quantity == 0:
            return ""
        return f"{quantity:,d}"

    if column.kind == COUNT:
        count = int(value)
        if column.blank_zero and count == 0:
            return ""
        return f"{count:,d}"

    if column.kind == PERCENT:
        whole, hundredths = divmod(int(value), 100)
        return f"{whole}.{hundredths:02d}%"

    if column.kind == DATE:
        return _as_date(value).strftime("%d %b %Y")

    return str(value)


def export(column: Column, value) -> str:
    """The cell as a spreadsheet parses it.

    Deliberately not :func:`display`. Thousands separators split a CSV column in
    two, a currency symbol makes the whole column text, and ``08 Aug 2026`` is a
    date only to a reader who already knows the locale. So: plain decimals,
    plain integers, ISO dates.

    Money crosses to rupees here and nowhere else in this app, through
    :func:`~apps.core.money.to_rupees` — the boundary CLAUDE.md §1 allows,
    because the value is on its way out of the system and never comes back.
    """
    if value is None or value == "":
        return ""

    if column.kind == MONEY:
        return f"{to_rupees(int(value))}"

    if column.kind == QTY:
        return value if isinstance(value, str) else str(int(value))

    if column.kind == COUNT:
        return str(int(value))

    if column.kind == PERCENT:
        whole, hundredths = divmod(int(value), 100)
        return f"{whole}.{hundredths:02d}"

    if column.kind == DATE:
        return _as_date(value).isoformat()

    return str(value)


def _as_date(value) -> dt.date:
    """A ``date`` from a ``date``, a ``datetime`` or an ISO string.

    ``datetime`` is narrowed with ``.date()`` rather than rejected: unlike a
    posting date — where which day a sale hit the books must not depend on a
    timezone conversion, see
    :func:`apps.accounting.services._as_ledger_date` — this value is on its way
    to being printed and has already been decided.
    """
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value))


def total_row(columns, rows, *, label: str = "Total", label_key: str = "") -> dict:
    """Sum every column flagged ``total`` across ``rows``.

    Returned as a plain dict keyed the same way a row is, so the totals line
    renders through exactly the same code path as an ordinary line — which is
    what stops a totals row drifting out of alignment with the column it totals.

    ``label_key`` is the column the word "Total" is written into; by default the
    first non-numeric column, which is where the eye looks for it.
    """
    columns = list(columns)
    totals: dict[str, object] = {}

    key = label_key or next((c.key for c in columns if not c.is_numeric), "")
    if key:
        totals[key] = label

    for column in columns:
        if not column.total:
            continue
        totals[column.key] = sum(int(row.get(column.key) or 0) for row in rows)
    return totals


__all__ = [
    "CODE",
    "COUNT",
    "DATE",
    "KINDS",
    "MONEY",
    "NUMERIC_KINDS",
    "PERCENT",
    "QTY",
    "TEXT",
    "Column",
    "ReportRow",
    "display",
    "export",
    "total_row",
]
