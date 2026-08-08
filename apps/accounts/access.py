"""Guarding a view, and hiding what a person cannot reach.

Two halves of one rule, and the rule is that **they must agree**. A menu that
offers a screen the click will refuse is worse than no menu: the person clicks,
gets a 403, and either files a bug or works out that the system is lying to
them. So the decorator and the navigation read the same permission names, from
:mod:`apps.accounts.permissions`.

    @module_required(perms.VIEW_REPORTS)          the view
    {% if perms.reports.view_reports %}           the template
    "permission": can(perms.VIEW_REPORTS)         the admin sidebar

``perms`` in a template is Django's own, from
``django.contrib.auth.context_processors.auth`` — nothing here adds a context
processor, because Django already ships the exact thing.

What "module-level access" means here
-------------------------------------
A module is a screen you can open, and the permission that decides it is the
ordinary ``view_`` on the model that screen is about: ``sales.view_salesinvoice``
opens the sales section. The lifecycle permissions (``post_`` / ``cancel_`` /
``amend_``) then guard the individual actions inside it. That split is what
lets an Operator live on the sales screen all day and be unable to reverse
anything on it.
"""

from __future__ import annotations

from functools import wraps

from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied


def model_permission(model, action: str) -> str:
    """``model_permission(SalesInvoice, "view")`` -> ``"sales.view_salesinvoice"``.

    Derived from the model rather than typed, for the same reason
    :meth:`apps.core.models.DocumentModel.cancel_permission` is: the sales and
    purchasing screens serve two document types from one set of views, and a
    hard-coded string there would guard the credit note with the invoice's
    permission and nobody would notice.
    """
    return f"{model._meta.app_label}.{action}_{model._meta.model_name}"


def has_access(user, *perms: str, any_of: bool = False) -> bool:
    """Whether ``user`` holds these permissions. ``all`` unless ``any_of``.

    An inactive user holds nothing, which Django's own backend already
    enforces; this is stated again because a deactivated login is the whole
    point of the deactivate button in the user admin, and it must not depend on
    a session expiring.
    """
    if user is None or not user.is_authenticated or not user.is_active:
        return False
    if not perms:
        return True
    checks = (user.has_perm(permission) for permission in perms)
    return any(checks) if any_of else all(checks)


def require(user, *perms: str, any_of: bool = False, doing: str = "") -> None:
    """Raise ``PermissionDenied`` unless ``user`` holds these permissions.

    The in-view companion to :func:`module_required`, for the screens whose
    permission is not known until the request is resolved — the sales and
    purchasing views serve two document types off one URL, so which permission
    applies depends on the slug.

    ``doing`` names the action in the refusal, because "you may not do this" is
    only useful if the person can tell somebody *what*.
    """
    if has_access(user, *perms, any_of=any_of):
        return
    joiner = " or " if any_of else " and "
    what = f"{doing} needs" if doing else "This needs"
    raise PermissionDenied(
        f"{what} the {joiner.join(repr(p) for p in perms)} permission. "
        f"Ask an administrator to put you in a group that has it."
    )


def module_required(*perms: str, any_of: bool = False):
    """Refuse this view to anybody who does not hold these permissions.

    Replaces ``@login_required`` rather than sitting next to it — an anonymous
    user is sent to the login screen with ``?next=``, exactly as before, and a
    logged-in user without the permission gets a 403 naming what they would
    need. Two different answers to two different situations: "you are not
    signed in" is fixable by the person, "you may not do this" is not.

        @module_required(perms.VIEW_REPORTS)
        @require_GET
        def report_index(request): ...

    ``any_of=True`` for a screen that more than one role reaches by different
    routes.
    """

    def decorate(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            user = request.user
            if not user.is_authenticated:
                return redirect_to_login(request.get_full_path())
            if not has_access(user, *perms, any_of=any_of):
                joiner = " or " if any_of else " and "
                raise PermissionDenied(
                    f"This screen needs the {joiner.join(repr(p) for p in perms)} "
                    f"permission. Ask an administrator to put you in a group that has it."
                )
            return view(request, *args, **kwargs)

        # Kept so a test — and a person reading the code — can ask a view what
        # it wants without calling it.
        wrapper.required_permissions = tuple(perms)
        wrapper.requires_any = any_of
        return wrapper

    return decorate


def can(*perms: str, any_of: bool = False):
    """A ``permission`` callable for a django-unfold sidebar entry.

    Unfold asks each navigation item whether to draw itself by calling this with
    the request. Using the same names the views use is what keeps the sidebar
    honest: a module the click would refuse is a module the sidebar does not
    draw.
    """

    def check(request) -> bool:
        return has_access(getattr(request, "user", None), *perms, any_of=any_of)

    return check


def can_staff(*perms: str, any_of: bool = False):
    """:func:`can`, and also "may open the admin at all".

    The admin sidebar is only ever rendered for staff, but an entry that checks
    it anyway is one that cannot be moved somewhere else and quietly start
    showing.
    """

    def check(request) -> bool:
        user = getattr(request, "user", None)
        if user is None or not user.is_active or not user.is_staff:
            return False
        return has_access(user, *perms, any_of=any_of)

    return check


__all__ = [
    "can",
    "can_staff",
    "has_access",
    "model_permission",
    "module_required",
    "require",
]
