"""Screens that belong to no single module.

Cancelling has its own URL inside each app (each one owns its service function
and its permission — see :func:`apps.core.views.cancel_view`), so what is left
here is the keyboard reference.
"""

from django.urls import path

from . import views

app_name = "core"

urlpatterns = [
    path("shortcuts/", views.shortcuts, name="shortcuts"),
]
