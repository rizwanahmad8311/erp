"""URLs for the payment screens and the recovery workspace.

Literal segments come first so ``recovery/`` and ``cheques/`` are never mistaken
for a document, and the HTMX endpoints sit under the screen they belong to —
each returns a rendered partial, and none of them returns JSON, because the
browser is not allowed to compute money from it.
"""

from django.urls import path

from . import views

app_name = "payments"

urlpatterns = [
    # The screen the accountant lives in.
    path("recovery/", views.workspace, name="recovery"),
    path("recovery/rows/", views.workspace_rows, name="recovery-rows"),
    path("recovery/clients/<int:pk>/", views.client_row, name="recovery-client"),
    path("recovery/clients/<int:pk>/receive/", views.client_receive, name="recovery-receive"),
    path("recovery/clients/<int:pk>/allocate/", views.client_allocate, name="recovery-allocate"),
    # The drawer.
    path("cheques/", views.cheque_register, name="cheques"),
    path("cheques/events/<int:pk>/cancel/", views.cheque_event_cancel, name="cheque-event-cancel"),
    # The autocomplete is not per-document: it is used before a payment exists.
    path("clients/search/", views.client_search, name="client-search"),
    # Receipts and payments.
    path("", views.payment_list, name="list"),
    path("new/", views.payment_create, name="create"),
    path("<int:pk>/", views.payment_detail, name="detail"),
    path("<int:pk>/post/", views.payment_post, name="post"),
    path("<int:pk>/cancel/", views.payment_cancel, name="cancel"),
    path("<int:pk>/amend/", views.payment_amend, name="amend"),
    path("<int:pk>/delete/", views.payment_delete, name="delete"),
    path("<int:pk>/allocate/", views.payment_allocate, name="allocate"),
    path("<int:pk>/allocate/oldest/", views.payment_auto_allocate, name="auto-allocate"),
    # Clearing and bouncing are two routes, not one route with a parameter. A
    # stale radio button — or a mistyped path — must not be able to choose
    # between "the bank took it" and "the bank sent it back".
    path("<int:pk>/cheque/clear/", views.cheque_clear, name="cheque-clear"),
    path("<int:pk>/cheque/bounce/", views.cheque_bounce, name="cheque-bounce"),
]
