"""Make a new user change the password somebody else chose for them.

A login created in the user admin is created with a password the administrator
typed and therefore knows. Until the person it belongs to changes it, "who
posted this invoice" has two possible answers, and an audit trail with two
possible answers is not one. So the flag is set when the login is created
(:attr:`apps.accounts.models.UserProfile.must_change_password`) and this
middleware refuses to let the session do anything else until it is cleared.

It is a redirect rather than a 403 because there is exactly one thing the person
can usefully do, and sending them straight to it is kinder than telling them
what they cannot do.

The exempt list is short on purpose. Anything reachable while the flag is set is
something an administrator's password can still be used for, so it is limited
to: the password change screen itself, logging out, logging in, and the static
files those pages need.
"""

from __future__ import annotations

from django.shortcuts import redirect
from django.urls import NoReverseMatch, resolve, reverse

from .models import UserProfile

#: URL names that stay reachable while a password change is outstanding.
EXEMPT_URL_NAMES = frozenset(
    {
        "admin:password_change",
        "admin:password_change_done",
        "admin:logout",
        "admin:login",
    }
)

#: Path prefixes that are never intercepted. Static and media are served by this
#: same process (there is no nginx on the office PC — CLAUDE.md §8), so a
#: redirect here would strip the stylesheet off the page it redirects to.
EXEMPT_PREFIXES = ("/static/", "/media/")


class ForcePasswordChangeMiddleware:
    """Redirect to the password change screen until the first password is set."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if self._must_redirect(request):
            return redirect(reverse("admin:password_change"))
        return self.get_response(request)

    # ------------------------------------------------------------------
    def _must_redirect(self, request) -> bool:
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return False

        path = request.path
        if path.startswith(EXEMPT_PREFIXES):
            return False

        try:
            match = resolve(path)
        except Exception:
            # An unresolvable path is a 404 on its way to being one. Let it go
            # rather than redirecting somebody to a password screen because they
            # mistyped a URL.
            return False

        if match.view_name in EXEMPT_URL_NAMES:
            return False

        # One row, and only for a logged-in user on a page that is not already
        # exempt. ``resolve_password_change`` clears the flag itself the moment
        # the hash differs from the one the administrator set, so there is no
        # second place that has to remember to — see
        # :meth:`apps.accounts.models.UserProfile.resolve_password_change`.
        return UserProfile.for_user(user).resolve_password_change(user)

    @staticmethod
    def password_change_url() -> str:
        """Where this middleware sends people. Named so a test cannot drift."""
        try:
            return reverse("admin:password_change")
        except NoReverseMatch:  # pragma: no cover - the admin is always installed
            return "/admin/password_change/"


__all__ = ["EXEMPT_PREFIXES", "EXEMPT_URL_NAMES", "ForcePasswordChangeMiddleware"]
