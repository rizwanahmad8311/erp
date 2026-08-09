"""What the operator sees when something the application did not expect happens.

Business refusals — over the credit limit, short of stock, a document something
depends on — are not this. Those are :class:`~apps.core.exceptions.CoreError`,
every view catches them, and they arrive as a sentence beside the field. This
module is for the other kind: a bug, a full disk, a database that has gone away.

Django's default for those is a page reading ``Server Error (500)`` and nothing
else. On a machine with no developer sitting at it, that is indistinguishable
from the network being down, and the usual next step is to try again, and then
to keep working and hope.

So three things happen instead:

1. **The traceback is logged**, in full, to ``logs/erp.log`` — which rotates and
   is capped, because this runs as a service on a PC nobody logs into for
   months (see ``config/settings/prod.py``).
2. **A short reference is generated** and logged next to the traceback.
3. **The page shows that reference** and says what to do. "Quote A7F3C2" turns
   an unreproducible phone call into a log search.

The reference is deliberately short and unambiguous to read aloud: eight
characters, no vowels, so it cannot spell anything and cannot be confused
between O and 0.
"""

from __future__ import annotations

import logging
import secrets

from django.shortcuts import render

logger = logging.getLogger("apps.core.errors")

#: No vowels (nothing spells a word), no 0/O, no 1/I/L — read over a phone.
_ALPHABET = "23456789BCDFGHJKMNPQRSTVWXZ"
REFERENCE_LENGTH = 8


def new_reference() -> str:
    """A short identifier for one failure, safe to read down a telephone."""
    return "".join(secrets.choice(_ALPHABET) for _ in range(REFERENCE_LENGTH))


class ErrorReferenceMiddleware:
    """Attach a reference to every request, and log it with any failure.

    Placed last in ``MIDDLEWARE`` so its ``process_exception`` runs **first** on
    the way out — Django unwinds exception hooks in reverse order — and the
    reference is therefore already logged before anything else handles it.

    It deliberately does not swallow the exception. Django's own handler still
    runs, ``django.request`` still logs the traceback at ERROR, and the response
    is still a 500; this only ensures the two can be tied together afterwards.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.error_reference = new_reference()
        return self.get_response(request)

    def process_exception(self, request, exception):
        reference = getattr(request, "error_reference", None) or new_reference()
        request.error_reference = reference
        logger.exception(
            "unhandled error [%s] %s %s for user %s",
            reference,
            request.method,
            request.get_full_path(),
            getattr(getattr(request, "user", None), "username", "anonymous"),
        )
        return None  # let Django carry on and produce the 500


def _render(request, template: str, status: int, **extra):
    """Render an error page, and never fail while doing it.

    An error template that raises turns a 500 into a 500 inside a 500, and
    Django falls back to a bare string. So this passes a minimal context and
    catches everything.
    """
    context = {"reference": getattr(request, "error_reference", None), **extra}
    try:
        return render(request, template, context, status=status)
    except Exception:  # pragma: no cover - only if the template itself is broken
        from django.http import HttpResponse

        logger.exception("the %s template failed to render", template)
        return HttpResponse(
            f"Something went wrong ({status}). Please tell whoever supports this system.",
            status=status,
            content_type="text/plain",
        )


def server_error(request, template_name="500.html"):
    """500. The traceback is already in the log with this reference beside it."""
    return _render(request, template_name, 500)


def not_found(request, exception, template_name="404.html"):
    """404. Usually a stale bookmark or a document that was deleted as a draft."""
    return _render(request, template_name, 404)


def permission_denied(request, exception, template_name="403.html"):
    """403. Names the permission where there is one, so an administrator can grant it."""
    return _render(request, template_name, 403, detail=str(exception) if exception else "")


def bad_request(request, exception, template_name="400.html"):
    """400. In practice: a CSRF token that expired because a tab sat open all night."""
    return _render(request, template_name, 400)


__all__ = [
    "ErrorReferenceMiddleware",
    "bad_request",
    "new_reference",
    "not_found",
    "permission_denied",
    "server_error",
]
