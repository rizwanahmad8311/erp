"""The company profile, on every page that might be printed.

A context processor rather than a per-view lookup because the print letterhead
is included from three apps' templates and a view that forgot to pass it would
print a bill with no company name on it — which is exactly the failure nobody
notices until a customer has the paper in their hand.

One query per request, and only when a template actually renders the header:
``CompanyProfile.get()`` is wrapped in a lazy object, so a page that never
mentions ``company`` never asks the database for it.
"""

from django.utils.functional import SimpleLazyObject

from .models import CompanyProfile


def company(request):
    """Adds ``company`` — the one :class:`~apps.reports.models.CompanyProfile`."""
    return {"company": SimpleLazyObject(CompanyProfile.get)}
