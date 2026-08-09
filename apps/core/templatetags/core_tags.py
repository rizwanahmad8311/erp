"""
Display filters. ``{% load core_tags %}``

Everything here is presentation only — nothing in this module is ever used to
compute a stored value (CLAUDE.md §1).
"""

from django import template
from django.utils.html import format_html

from apps.core import money as money_module
from apps.core import words as words_module
from apps.core.enums import DocumentStatus

register = template.Library()

#: Chip classes defined in static/src/css/app.css.
#:
#: Draft is **outlined**, not filled: a draft has written nothing to any ledger,
#: and a solid colour implies it has. Posted is signal, cancelled is alarm —
#: and cancelled is one of the very few places rust appears at all, so that a
#: reversal is findable by scanning rather than by reading.
_STATUS_CLASSES = {
    DocumentStatus.DRAFT: "chip chip-draft",
    DocumentStatus.POSTED: "chip chip-posted",
    DocumentStatus.CANCELLED: "chip chip-cancelled",
}


@register.filter(name="money")
def money(paisa) -> str:
    """``{{ line.amount_paisa|money }}`` -> ``1,234.50``

    No currency symbol: amount columns align better without one, and invoices
    print the symbol in the column header. An empty value renders as an empty
    string rather than 0.00, so a blank cell stays blank.
    """
    if paisa is None or paisa == "":
        return ""
    return money_module.fmt(int(paisa))


@register.filter(name="words")
def words(paisa) -> str:
    """``{{ invoice.total_paisa|words }}`` -> ``"Rupees One Lakh … Only"``

    Lakh and crore, not million — see :mod:`apps.core.words`. Printed under the
    total on an invoice, on paper and on screen, so the browser's own print and
    the ReportLab PDF say the same sentence.
    """
    if paisa is None or paisa == "":
        return ""
    return words_module.amount_in_words(int(paisa))


@register.filter(name="qty")
def qty(pieces) -> str:
    """``{{ line.qty_pieces|qty }}`` -> ``1,200``

    Base units, always a whole number (CLAUDE.md §2).
    """
    if pieces is None or pieces == "":
        return ""
    return f"{int(pieces):,d}"


@register.filter(name="doc_status")
def doc_status(status) -> str:
    """``{{ invoice.status|doc_status }}`` -> a coloured badge.

    Accepts a status string or anything whose ``str()`` is one. An unknown value
    renders in neutral styling rather than raising, so a half-migrated row can
    still be looked at in the admin.
    """
    value = str(status or "")
    try:
        label = DocumentStatus(value).label
    except ValueError:
        label = value or "—"
    css = _STATUS_CLASSES.get(value, "chip chip-draft")
    return format_html('<span class="{}">{}</span>', css, label)
