"""Per-user UI settings.

Nothing here touches money, a ledger or a document, so none of CLAUDE.md §1 to
§6 is in play. It is kept in ``accounts`` because the value lives on
:class:`~apps.accounts.models.UserProfile`.
"""

from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.views.decorators.http import require_POST

from .models import Density, UserProfile


#: Where a density switch may send somebody afterwards. A bare ``next`` off the
#: query string is an open redirect; this screen has exactly one legitimate
#: destination pattern — back where you were — so it is checked rather than
#: trusted.
def _safe_next(request) -> str:
    candidate = request.POST.get("next") or ""
    if candidate.startswith("/") and not candidate.startswith("//"):
        return candidate
    return reverse("core:shortcuts")


@require_POST
@login_required
def set_density(request):
    """Switch this login between comfortable and compact.

    POST only. A GET that changed a stored setting would be changed by a
    prefetch, a bookmark or the back button.

    An unrecognised value falls back to comfortable rather than raising: this is
    a display preference, and a 500 on the way back from a mistyped form post
    would lose whatever the operator was in the middle of.
    """
    value = request.POST.get("density", "")
    if value not in Density.values:
        value = Density.COMFORTABLE

    profile = UserProfile.for_user(request.user)
    if profile.density != value:
        profile.density = value
        profile.save(update_fields=["density", "updated_at"])

    return HttpResponseRedirect(_safe_next(request))
