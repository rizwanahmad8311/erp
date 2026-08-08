"""The one view every report is served by.

Eighteen reports, one view, one template, one filter bar, three formats. A
report contributes a column list and a ``build`` function
(:mod:`apps.reports.registry`); everything on this page — the bar, the pager,
the CSV link, the PDF link, the cancelled toggle, the "these totals are the
whole set, not this page" note — is here and is therefore the same on all of
them.

    /reports/                        the index
    /reports/trial-balance/          HTML
    /reports/trial-balance/?format=csv
    /reports/trial-balance/?format=pdf

Three decisions worth knowing about.

**Pagination applies to the screen and to nothing else.** A CSV is a data file
and a paginated one is a data file somebody has to reassemble by hand; a PDF is
a document, so it is capped and *says on the page* when it truncated
(:data:`apps.reports.pdf.reports.MAX_PDF_ROWS`). Neither ever paginates the
totals: ``build`` computes them over the complete set before the page is sliced,
because a totals line that quietly meant "this page" is the most dangerous thing
a report can print.

**A missing required filter is a question, not an empty table.** A General
Ledger with no account chosen has no rows, and rendering it as an empty grid
implies the account has no entries. The screen asks instead.

**Every screen carries the cancelled toggle**, and the default is off — figures
leave reversed documents out, listings show them when asked (CLAUDE.md §5).
Nothing on this page writes anything, and nothing here opens a transaction:
CLAUDE.md §4 forbids PDF rendering inside one, and a read has no business
holding SQLite's write lock.
"""

from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import render
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.http import require_GET

from .columns import display
from .criteria import ReportFilterForm
from .exports import csv_filename, csv_response
from .pdf.reports import report_pdf
from .registry import get_report, grouped
from .responses import pdf_filename, pdf_response, wants_download

#: Rows on a screen. Enough that a route's shops fit on one page; few enough
#: that a browser on the office PC renders it instantly.
PAGE_SIZE = 100

#: The formats a report can be asked for. HTML is the empty string, so a bare
#: URL is the screen.
FORMATS = {"", "csv", "pdf"}


def _format(request) -> str:
    value = (request.GET.get("format") or "").strip().lower()
    if value not in FORMATS:
        raise Http404(f"Unknown report format {value!r}; expected csv or pdf.")
    return value


@method_decorator(login_required, name="dispatch")
@method_decorator(require_GET, name="dispatch")
class ReportView(View):
    """Runs one registered report and renders it in the format asked for."""

    template_name = "reports/report.html"

    def get(self, request, slug: str):
        report = get_report(slug)
        if report is None:
            raise Http404(f"No report is registered under {slug!r}.")

        form = ReportFilterForm(report, request.GET)
        criteria = form.criteria()

        missing = report.missing_requirements(criteria)
        if missing:
            return self._ask(request, report, form, criteria, missing)

        result = report.build(criteria)

        output = _format(request)
        if output == "csv":
            return csv_response(
                report,
                result,
                filename=csv_filename(report.slug, *_filename_parts(report, criteria)),
            )
        if output == "pdf":
            return pdf_response(
                report_pdf(report, result, criteria),
                pdf_filename(report.slug, *_filename_parts(report, criteria)),
                download=wants_download(request),
            )

        return render(
            request,
            self.template_name,
            self._context(request, report, form, criteria, result),
        )

    # ------------------------------------------------------------------
    def _ask(self, request, report, form, criteria, missing):
        """The screen a report shows before it has been told what to run for."""
        labels = [form.fields[name].label or name for name in missing]
        prompt = (
            f"Choose {' and '.join(labels).lower()} above to run this report. "
            f"An empty table would read as “nothing here”, which is not the same answer."
        )
        return render(
            request,
            self.template_name,
            {
                **self._base_context(request, report, form, criteria),
                "result": None,
                "rows": [],
                "page_obj": None,
                "prompt": prompt,
            },
        )

    def _base_context(self, request, report, form, criteria) -> dict:
        base = reverse("reports:report", kwargs={"slug": report.slug})
        return {
            "report": report,
            "reports": grouped(),
            "form": form,
            "criteria": criteria,
            "columns": report.columns,
            "base_url": base,
            "csv_url": f"{base}?{form.querystring(format='csv')}",
            "pdf_url": f"{base}?{form.querystring(format='pdf')}",
            "pdf_download_url": f"{base}?{form.querystring(format='pdf', download=1)}",
            "toggle_url": f"{base}?{form.querystring(include_cancelled=None if criteria.include_cancelled else 1)}",
            "period_was_swapped": getattr(form, "period_was_swapped", False),
        }

    def _context(self, request, report, form, criteria, result) -> dict:
        paginator = Paginator(result.rows, PAGE_SIZE)
        page = paginator.get_page(request.GET.get("page"))
        return {
            **self._base_context(request, report, form, criteria),
            "result": result,
            # Formatted here rather than in the template, so the screen and the
            # PDF go through the same :func:`apps.reports.columns.display`. A
            # figure that reads one way in the browser and another on the
            # printout is a figure somebody reconciles the hard way.
            "rows": _rendered_rows(report, page.object_list),
            "page_obj": page,
            "paginator": paginator,
            "prompt": "",
            "total_cells": _rendered_totals(report, result),
        }


def _rendered_rows(report, rows) -> list[dict]:
    """Each row as its cells, already formatted, in column order."""
    rendered = []
    for row in rows:
        rendered.append(
            {
                "row": row,
                "cells": [
                    {
                        "column": column,
                        "text": display(column, row.get(column.key)),
                        "alarm": column.key in row.alarm,
                        # Only the column that carries the document code links,
                        # and only when the row actually references one.
                        "url": row.url if (column.link and row.url) else "",
                    }
                    for column in report.columns
                ],
            }
        )
    return rendered


def _rendered_totals(report, result) -> list[dict]:
    """The totals line, already formatted, one entry per column."""
    if not result.totals:
        return []
    return [
        {
            "column": column,
            "text": display(column, result.totals.get(column.key))
            if column.key in result.totals
            else "",
        }
        for column in report.columns
    ]


def _filename_parts(report, criteria) -> list[str]:
    """What goes in the downloaded file's name after the report's own slug.

    The period, and whichever party or account the report was run for — because
    a Downloads folder with four files called ``client-ledger.csv`` in it is a
    folder nobody can use.
    """
    parts: list[str] = []
    for name in ("account", "client", "vendor", "route", "seller", "item", "warehouse"):
        value = getattr(criteria, name, None)
        if name in report.filters and value is not None:
            parts.append(str(getattr(value, "code", value)))
    if "as_of" in report.filters:
        parts.append(criteria.as_of.isoformat())
    else:
        parts += [criteria.date_from.isoformat(), criteria.date_to.isoformat()]
    return parts


@login_required
@require_GET
def report_index(request):
    """Every report this system has, grouped the way the office talks about them."""
    return render(request, "reports/index.html", {"reports": grouped()})


__all__ = ["FORMATS", "PAGE_SIZE", "ReportView", "report_index"]
