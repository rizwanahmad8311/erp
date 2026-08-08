"""Row-level scope: a booker sees their own routes and nobody else's.

Permissions answer "may this person open the recovery screen". They cannot
answer "may this person open *this shop's* row", because Django's permissions
are per model, not per row. That second question is this module.

The rule, in one line::

    a user without accounts.view_all_routes sees only the routes their
    seller is assigned to through RouteSeller

and everything below is that rule applied to whatever queryset a view is about
to hand back.

Three things about the design are deliberate.

**It is applied at the view layer, not in a manager.** A default manager that
silently filtered would also filter the posting services, the reports, the
ledger aggregations and the admin — all of which must see everything, because a
trial balance that depended on who was logged in is not a trial balance. Making
it explicit at the view means every scoped screen names the fact that it is
scoped, and an unscoped one is visibly unscoped.

**A scoped user with no seller sees nothing.** Not everything. If the link from
a login to a booker has not been set up, the safe answer is an empty list and a
sentence explaining it, because the unsafe answer is the whole customer book.

**It scopes objects, not just lists.** :func:`scoped_get_object_or_404` is the
half that matters: hiding a shop from a list and then serving it to anybody who
types its id is not access control, it is decoration. The test that proves this
is ``tests/test_permissions.py::TestBookerRowScope``.
"""

from __future__ import annotations

from django.http import Http404
from django.shortcuts import get_object_or_404

from .models import UserProfile
from .permissions import VIEW_ALL_ROUTES


def is_route_scoped(user) -> bool:
    """Whether this user is limited to their own routes.

    Superusers and anybody holding :data:`~apps.accounts.permissions.VIEW_ALL_ROUTES`
    are not. Everybody else is — including a user with no profile and no seller,
    who consequently sees nothing at all. That default is the point: a login
    that has not been set up is not a login that gets the whole customer book.
    """
    if user is None or not user.is_authenticated:
        return True
    return not user.has_perm(VIEW_ALL_ROUTES)


def visible_route_ids(user) -> list[int] | None:
    """The routes this user may see, or ``None`` meaning "all of them".

    ``None`` rather than a list of every route id: the callers use it to decide
    whether to filter at all, and materialising every route to then not filter
    on it would be a query per screen for nothing.
    """
    if not is_route_scoped(user):
        return None
    if user is None or not user.is_authenticated:
        return []
    return UserProfile.for_user(user).route_ids


def scope_queryset(queryset, user, *, route_field: str = "route"):
    """Narrow ``queryset`` to the routes this user may see.

    ``route_field`` is the path from the model to its route:

        clients            ``"route"``
        sales invoices     ``"route"`` — the beat the bill was booked on
        payments           ``"route"`` — the beat the money was collected on

    A scoped user with no routes gets ``queryset.none()``, which is an empty
    page saying so rather than a page of somebody else's shops.
    """
    route_ids = visible_route_ids(user)
    if route_ids is None:
        return queryset
    if not route_ids:
        return queryset.none()
    return queryset.filter(**{f"{route_field}__in": route_ids})


def scope_clients(queryset, user):
    """Clients on the routes this user may see. The common case, named."""
    return scope_queryset(queryset, user, route_field="route")


def scoped_get_object_or_404(queryset, user, *, route_field: str = "route", **lookup):
    """``get_object_or_404`` that cannot be walked around by typing an id.

    The scope is applied to the queryset **before** the lookup, so a row outside
    it is a 404 and not a 403 — which is deliberate. A 403 on a specific shop id
    confirms that the shop exists and that somebody else has it; a 404 says
    nothing, and "nothing" is the correct amount to tell a booker about another
    beat's customer.
    """
    return get_object_or_404(scope_queryset(queryset, user, route_field=route_field), **lookup)


class RouteScopedQuerySetMixin:
    """Route scoping for a class-based view, and a note on the ones that must not.

    Set :attr:`route_field` to the path from the model to its route, then call
    :meth:`scoped` on whatever queryset the view was about to use::

        class ClientList(RouteScopedQuerySetMixin, ListView):
            route_field = "route"

            def get_queryset(self):
                return self.scoped(Client.objects.select_related("route"))

    Reports do **not** use this. A figure that changed depending on who was
    looking at it is not a figure — a trial balance is the same trial balance
    for everybody or it is nothing (CLAUDE.md §6). What limits a booker there is
    which reports they may open at all, not which rows the arithmetic covers.
    """

    #: The path from this view's model to its route.
    route_field: str = "route"

    def scoped(self, queryset):
        return scope_queryset(queryset, self.request.user, route_field=self.route_field)

    def scoped_object(self, queryset, **lookup):
        return scoped_get_object_or_404(
            queryset, self.request.user, route_field=self.route_field, **lookup
        )


def assert_in_scope(obj, user, *, route_attr: str = "route_id") -> None:
    """Raise ``Http404`` unless this already-loaded object is in the user's scope.

    For the handful of places that have the object before they can filter for
    it — an HTMX partial that was handed a client, a service the view already
    fetched. Prefer :func:`scoped_get_object_or_404`, which cannot be forgotten.
    """
    route_ids = visible_route_ids(user)
    if route_ids is None:
        return
    if getattr(obj, route_attr, None) not in route_ids:
        raise Http404("No such record on your routes.")


__all__ = [
    "RouteScopedQuerySetMixin",
    "assert_in_scope",
    "is_route_scoped",
    "scope_clients",
    "scope_queryset",
    "scoped_get_object_or_404",
    "visible_route_ids",
]
