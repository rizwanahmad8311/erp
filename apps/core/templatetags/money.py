"""Template filters for rendering integer paisa. Display only."""

from django import template
from django.conf import settings

from apps.core import money

register = template.Library()


@register.filter(name="money")
def money_filter(paisa) -> str:
    """{{ line.amount_paisa|money }} -> 'Rs 1,234.50'"""
    if paisa is None:
        return ""
    return money.format_money(int(paisa), symbol=settings.CURRENCY_SYMBOL)


@register.filter(name="rupees")
def rupees_filter(paisa) -> str:
    """Bare number, no symbol — for right-aligned table columns."""
    if paisa is None:
        return ""
    return f"{money.to_rupees(int(paisa)):,.2f}"
