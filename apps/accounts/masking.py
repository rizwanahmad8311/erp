"""Hiding what things cost, in every place a figure can leave the building.

``masters.view_cost_price`` is the one permission that changes what a page
*contains* rather than whether it opens. That makes it the one that leaks,
because there are three ways out of this system and it is easy to guard one:

    the screen      a template that skips the column
    the CSV         which is the usual leak — the column is gone from the table
                    and still in the file, because the export was written from
                    the column list rather than from the template
    the PDF         same, one layer further away

So masking is decided **once**, here, by :func:`may_see_cost`, and applied where
the columns are chosen rather than where they are drawn — see
:meth:`apps.reports.registry.Report.columns_for`, which every one of the three
formats goes through. A column the user may not see is not blanked, it is
**absent**: a masked column still tells you a cost exists and roughly where, and
a blank column in a CSV invites somebody to ask why.

``tests/test_permissions.py::TestCostPriceMasking`` asserts the figure is
missing from all three, which is the test that would have caught the export.
"""

from __future__ import annotations

from .permissions import VIEW_COST_PRICE

#: What a masked figure reads as on a screen where the row still has to line up.
#: Never a zero and never an empty cell: both look like an answer, and this is
#: the absence of one.
MASK = "—"


def may_see_cost(user) -> bool:
    """Whether this user may be shown cost prices, margins and valuation.

    ``None`` counts as **allowed**, and that is deliberate but narrow: a caller
    with no user is a management command, a data migration or a PDF rendered by
    a scheduled job, all of which are already trusted code. Everything reachable
    from a browser passes ``request.user``, which is never ``None``.
    """
    if user is None:
        return True
    if not user.is_authenticated:
        return False
    return user.has_perm(VIEW_COST_PRICE)


def mask_cost(value, user, *, mask: str = MASK):
    """``value`` if this user may see cost, the mask otherwise.

    For the handful of single figures that are not part of a column list — the
    cost-of-goods line on the posting strip, a margin on a detail page.
    """
    return value if may_see_cost(user) else mask


__all__ = ["MASK", "mask_cost", "may_see_cost"]
