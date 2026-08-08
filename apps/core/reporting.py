"""What a report may read, and what it must leave out.

A cancelled document is never hidden and never deleted (CLAUDE.md §5) — it is
part of the audit trail and it stays on every list screen, watermarked. What it
must not do is turn up in a **figure**. Its ledger rows and their reversals net
to zero, so an aggregate over the ledger already ignores it; anything that reads
*documents* has to be told, and this is where it is told once.

    SalesInvoice.objects.live()                  what a report counts
    SalesInvoice.objects.cancelled()             the audit view
    SalesInvoice.objects.for_report(include_cancelled=flag)

``live()`` is the default for every report-shaped read, and
``tests/test_lifecycle.py`` fails the build if a document model stops offering
it. The toggle exists because "show me what was cancelled" is a real question
with a real answer, asked by somebody reconciling a month — it is opt-in and it
says so on screen.

Note what ``live()`` is **not**: it is not a filter for list screens. A list
that quietly dropped cancelled documents would hide the correction somebody is
looking for, which is the opposite of an audit trail.
"""

from __future__ import annotations

from django.db import models

from .enums import DocumentStatus

#: The query-string flag every screen uses for the audit toggle. One spelling,
#: so a bookmarked URL works on any of them.
INCLUDE_CANCELLED_PARAM = "include_cancelled"


class DocumentQuerySet(models.QuerySet):
    """The three questions a report asks about document status."""

    def live(self):
        """Everything a figure may count: DRAFT and POSTED, never CANCELLED.

        Drafts are in because a draft that has written nothing is still a real
        document somebody is working on, and a screen listing "what is in
        progress" is not a financial figure. Anything aggregating **money**
        should filter to POSTED itself — the ledger is the source of truth for
        that (CLAUDE.md §6), and it only ever holds posted rows.
        """
        return self.exclude(status=DocumentStatus.CANCELLED)

    def cancelled(self):
        """Only the reversed ones. The audit view, asked for explicitly."""
        return self.filter(status=DocumentStatus.CANCELLED)

    def posted(self):
        """Only the ones that are actually in the books right now."""
        return self.filter(status=DocumentStatus.POSTED)

    def for_report(self, *, include_cancelled: bool = False):
        """``live()`` unless the caller has explicitly asked for the audit view."""
        return self if include_cancelled else self.live()


def include_cancelled_from(request) -> bool:
    """Whether this request ticked the audit toggle.

    Anything other than the flag being present and truthy means no, so a stray
    ``?include_cancelled=`` on a bookmarked URL does not quietly widen a report.
    """
    value = (request.GET.get(INCLUDE_CANCELLED_PARAM) or "").strip().lower()
    return value in {"1", "true", "yes", "on"}


__all__ = [
    "INCLUDE_CANCELLED_PARAM",
    "DocumentQuerySet",
    "include_cancelled_from",
]
