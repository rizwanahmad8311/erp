"""URLs for the purchase screens.

One set of routes serving both document types, distinguished by ``slug`` —
``invoices`` or ``returns``. The views resolve it through
:data:`apps.purchasing.views.KINDS`, so an unknown slug is a 404 rather than a
screen that half works.
"""

from django.urls import path

from . import views

app_name = "purchasing"

urlpatterns = [
    path("<slug:slug>/", views.document_list, name="list"),
    path("<slug:slug>/new/", views.document_create, name="create"),
    path("<slug:slug>/<int:pk>/", views.document_detail, name="detail"),
    # HTMX endpoints. Each returns a rendered partial; none of them return JSON,
    # because the client is not allowed to compute money from it.
    path("<slug:slug>/<int:pk>/lines/", views.line_add, name="line-add"),
    path("<slug:slug>/<int:pk>/lines/preview/", views.line_preview, name="line-preview"),
    path(
        "<slug:slug>/<int:pk>/lines/<int:line_pk>/delete/",
        views.line_delete,
        name="line-delete",
    ),
    # Lifecycle. All POST, all straight through to a service.
    path("<slug:slug>/<int:pk>/post/", views.document_post, name="post"),
    path("<slug:slug>/<int:pk>/cancel/", views.document_cancel, name="cancel"),
    path("<slug:slug>/<int:pk>/amend/", views.document_amend, name="amend"),
    path("<slug:slug>/<int:pk>/delete/", views.document_delete, name="delete"),
]
