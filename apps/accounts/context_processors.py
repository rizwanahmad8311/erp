"""The per-user UI settings that have to be known before the first paint.

Density is written onto ``<html data-density="...">`` by ``templates/base.html``
rather than toggled by a class after load. That ordering is the whole point: a
setting applied by JavaScript arrives after the browser has already laid the
page out, so every screen visibly reflows once on every navigation — which on a
screen somebody stares at for eight hours is worse than not having the setting.

A context processor rather than a mixin on every view, for the same reason
:func:`apps.reports.context_processors.company` is one: it is needed by the base
template on every page, and a view that forgot to pass it would silently render
at the wrong density instead of failing.

One query per request for a signed-in user, and none for an anonymous one.
"""

from __future__ import annotations

import json

from django.utils.functional import SimpleLazyObject

from apps.core.shortcuts import KEY_MAP

from .models import Density

#: Serialised once at import. Django escapes it into the attribute on render.
_SHORTCUT_KEYS_JSON = json.dumps(KEY_MAP)


def _density_for(request) -> str:
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return Density.COMFORTABLE
    from .models import UserProfile

    return UserProfile.for_user(user).density


def ui(request):
    """Adds ``density`` and ``shortcut_keys`` — what ``base.html`` needs.

    ``density`` is ``"comfortable"`` or ``"compact"`` for this login, and is
    lazy: a response that never renders the base template — an htmx partial, a
    CSV, a PDF — never touches ``UserProfile``.

    ``shortcut_keys_json`` is the ``{"alt+n": "new", ...}`` map that ``app.js``
    binds, from :data:`apps.core.shortcuts.KEY_MAP`. It is a module-level
    constant serialised once at import, so it costs nothing per request and does
    not need to be lazy.

    It is JSON in a ``data-`` attribute rather than a ``json_script`` block
    because the entry screens are guarded against **any** inline script content
    — the guard that keeps money arithmetic out of the browser — and that guard
    is only worth having while it is absolute.

    Both ride here rather than in a processor each, because both are the same
    thing: the chrome the base template draws on every page. A second processor
    is a second entry somebody has to remember to add to ``TEMPLATES``.
    """
    return {
        "density": SimpleLazyObject(lambda: _density_for(request)),
        "shortcut_keys_json": _SHORTCUT_KEYS_JSON,
    }
