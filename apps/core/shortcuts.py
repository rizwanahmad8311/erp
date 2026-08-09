"""The keyboard layer, defined once.

This tuple is the **only** definition of what the keys do. Three things read it
and none of them keeps its own copy:

* ``templates/core/shortcuts.html`` — the ``/shortcuts`` page;
* ``templates/base.html`` — serialises it into a ``<script type="application/json">``
  block that ``static/src/js/app.js`` binds at runtime;
* the hint printed on the buttons themselves (``Post (Alt+P)``).

A keyboard map documented in one place and implemented in another is a map that
is wrong within a month, and the person it is wrong for is the operator who
learned it.

**Why ``action`` is a data attribute and not a selector.** Each binding names an
action, and a screen opts in by marking the control ``data-action="post"``. A
screen with no such control simply has no binding — which is why Alt+P does
nothing on a report and does not need a per-screen exception list.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Shortcut:
    """One binding.

    ``keys`` is what the operator presses, in the form the JS matches:
    ``alt+n``, ``enter``, ``escape``. ``action`` is the ``data-action`` value the
    key drives. ``scope`` is prose — where it applies — and exists so the
    ``/shortcuts`` page can say "entry screens" rather than listing URLs.
    """

    keys: str
    label: str
    action: str
    scope: str
    note: str = ""


SHORTCUTS: tuple[Shortcut, ...] = (
    Shortcut(
        keys="alt+n",
        label="Alt+N",
        action="new",
        scope="Sales, purchases, payments",
        note="Starts a new invoice as a draft. Nothing is written to any ledger yet.",
    ),
    Shortcut(
        keys="alt+s",
        label="Alt+S",
        action="save",
        scope="Any entry screen",
        note="Saves the draft where it is. Safe to press at any point.",
    ),
    Shortcut(
        keys="alt+p",
        label="Alt+P",
        action="post",
        scope="A draft document",
        note=(
            "Posts the document — writes the ledger and stock entries shown in the "
            "posting strip. A posted document cannot be edited afterwards, only "
            "cancelled and reversed."
        ),
    ),
    Shortcut(
        keys="alt+f",
        label="Alt+F",
        action="search",
        scope="Every screen with a search box",
        note="Puts the cursor in the search field and selects what is already there.",
    ),
    Shortcut(
        keys="enter",
        label="Enter",
        action="next-line",
        scope="The entry grid",
        note="Commits the line being typed and opens the next one.",
    ),
    Shortcut(
        keys="escape",
        label="Esc",
        action="cancel-edit",
        scope="The entry grid, any dialog",
        note="Abandons the line being edited and leaves the saved lines alone.",
    ),
)

#: What ``base.html`` serialises for the JS: ``{"alt+n": "new", ...}``.
KEY_MAP: dict[str, str] = {s.keys: s.action for s in SHORTCUTS}


__all__ = ["KEY_MAP", "SHORTCUTS", "Shortcut"]
