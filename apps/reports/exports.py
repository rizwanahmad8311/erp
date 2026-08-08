"""The CSV half of "three output formats".

A report's CSV is the same columns as its screen, written the way a spreadsheet
reads them: no thousands separators, no currency symbol, ISO dates. See
:func:`apps.reports.columns.export` for why display and export differ.

What is deliberately **not** in the file is a title block. A CSV that opens with
"General Ledger — 1 Apr to 30 Apr" and a blank line puts the headers on row 3,
which every tool that reads a CSV programmatically gets wrong. The period lives
in the filename instead, where it survives being mailed and filed.

Formula injection
-----------------
A cell whose text begins ``=``, ``+``, ``-`` or ``@`` is a formula to Excel and
to LibreOffice, and a client name typed as ``=cmd|...`` is a real attack on the
person who opens the file rather than on this system. Text cells that begin with
one of those are prefixed with an apostrophe, which those tools strip on
display. Numeric columns are untouched: a negative balance is a minus sign and
must stay one.
"""

from __future__ import annotations

import csv
import io

from django.http import HttpResponse

from .columns import export

#: Excel and LibreOffice treat a leading one of these as the start of a formula.
_FORMULA_LEADERS = ("=", "+", "-", "@", "\t", "\r")


def _safe(text: str, *, numeric: bool) -> str:
    """A cell a spreadsheet will show rather than evaluate."""
    if numeric or not text:
        return text
    return f"'{text}" if text[0] in _FORMULA_LEADERS else text


def write_csv(report, result, *, columns=None, stream=None) -> str:
    """The report as CSV text: headers, rows, then the totals line.

    The totals row is last and is labelled, so a file read by a human and a file
    read by a script disagree about it in the obvious way rather than the subtle
    one — a script that sums the column will double-count and notice, instead of
    quietly agreeing with a total it did not compute.
    """
    # ``columns`` rather than ``report.columns``: a cost column the user may
    # not see must be absent from the **file**, not merely from the screen.
    # This is the leak the whole masking design exists to close — see
    # apps.reports.registry.Report.columns_for.
    columns = report.columns if columns is None else tuple(columns)

    stream = stream if stream is not None else io.StringIO()
    writer = csv.writer(stream, lineterminator="\n")

    writer.writerow([column.label for column in columns])

    for row in result.rows:
        writer.writerow(
            [
                _safe(export(column, row.get(column.key)), numeric=column.is_numeric)
                for column in columns
            ]
        )

    if result.totals:
        writer.writerow(
            [
                _safe(export(column, result.totals.get(column.key)), numeric=column.is_numeric)
                if column.key in result.totals
                else ""
                for column in columns
            ]
        )

    return stream.getvalue()


def csv_response(report, result, *, filename: str, columns=None) -> HttpResponse:
    """The CSV as a download.

    A BOM is written ahead of the text. Without it Excel on Windows — which is
    every machine this file will be opened on — reads the bytes as the system
    codepage, and a shop called "Nadeem & Sons Kiryāna" arrives mangled. The BOM
    is what makes Excel read UTF-8, and ``utf-8-sig`` is where it comes from.
    """
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.write(write_csv(report, result, columns=columns).encode("utf-8-sig"))
    return response


def csv_filename(*parts: str) -> str:
    """``csv_filename("trial-balance", "2026-06-30")`` -> ``trial-balance-2026-06-30.csv``."""
    from .responses import pdf_filename

    return pdf_filename(*parts)[: -len(".pdf")] + ".csv"


__all__ = ["csv_filename", "csv_response", "write_csv"]
