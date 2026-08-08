"""URLs for the reports.

Two shapes, and the difference between them is what the paper is *for*.

**The catalogue** — ``/reports/`` and ``/reports/<slug>/`` — is every registered
report, each served by the one :class:`~apps.reports.framework.ReportView` in
HTML, CSV or PDF off the same URL. A report is a screen somebody sits at and
filters, and the file is what they take away from it.

**The two printed documents** — a client statement and a route recovery sheet —
are PDF-only, because they are pieces of paper somebody carries rather than
screens somebody sits at. They predate the catalogue and are kept at their own
URLs: they are linked from the recovery workspace and the client screens, and a
bookmark somebody made is not worth breaking to save a route entry. The
catalogue's ``client-ledger`` report is the same figures with a filter bar and a
CSV on it.

Literal segments come before ``<slug:slug>`` so ``clients/`` and ``routes/`` are
never mistaken for a report name.
"""

from django.urls import path

from . import views
from .framework import ReportView, report_index

app_name = "reports"

urlpatterns = [
    # The printed documents, PDF only.
    path("clients/<int:pk>/ledger/", views.client_ledger, name="client-ledger"),
    path("routes/<int:pk>/day-sheet/", views.route_day_sheet, name="route-day-sheet"),
    # The catalogue.
    path("", report_index, name="index"),
    path("<slug:slug>/", ReportView.as_view(), name="report"),
]
