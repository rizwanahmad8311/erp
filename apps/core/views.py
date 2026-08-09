"""The cancel screen, once, for every document type.

Cancelling is the same four questions whatever the document is, and three of
them are refusals:

1. **May this person cancel this kind of document?** ``<app>.cancel_<model>``,
   derived from the model rather than typed into each view — see
   :meth:`~apps.core.models.DocumentModel.cancel_permission`.
2. **Is anything hanging off it?** :meth:`~apps.core.models.DocumentModel.dependents`,
   listed on the screen and named in the refusal.
3. **What exactly would this write?** The reversing rows, from
   :func:`~apps.accounting.services.preview_reversal`, which mirrors the same
   live rows the cancellation itself mirrors. Shown *before* the button.
4. **Why?** A typed reason of at least
   :data:`~apps.core.forms.MIN_CANCEL_REASON` characters.

Three copies of that would be three screens that answer it differently, and the
one nobody looked at would be the one missing the permission check. So the apps
keep their own URLs and their own service function, and hand both to
:func:`cancel_view`.
"""

from __future__ import annotations

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect, render

from apps.accounting.services import preview_reversal

from .enums import DocumentStatus
from .exceptions import CoreError
from .forms import CancelForm

#: The one template all three apps render. It knows nothing about invoices.
CANCEL_TEMPLATE = "core/document_cancel.html"


def cancel_view(request, document, *, cancel, back_url, title, extra=None):
    """GET shows what cancelling would do; POST does it.

    ``cancel`` is the app's own service function, called as
    ``cancel(document, user=..., reason=...)``. This never writes a ledger row
    itself — it decides whether the service may be called, and shows what will
    happen if it is.

    ``back_url`` is where both the "leave it alone" link and the redirect after
    a cancellation go: the document's own screen, which by then carries the
    CANCELLED watermark and the timeline entry.
    """
    if not document.user_may_cancel(request.user):
        raise PermissionDenied(
            f"Cancelling a {document._meta.verbose_name} needs the "
            f"'{document.cancel_permission()}' permission."
        )

    if document.status != DocumentStatus.POSTED:
        messages.error(
            request,
            f"{document.code} is {document.status}. Only a posted document can be cancelled.",
        )
        return redirect(back_url)

    blockers = document.dependents()
    form = CancelForm(request.POST or None)

    if request.method == "POST":
        if blockers:
            # Ask the document itself rather than wording a second refusal here,
            # so the screen and the service say the same sentence.
            try:
                document.assert_cancellable()
            except CoreError as exc:
                messages.error(request, str(exc))
        elif form.is_valid():
            try:
                cancel(document, user=request.user, reason=form.cleaned_data["reason"])
            except CoreError as exc:
                # The service refuses on the same grounds this screen checks and
                # it is the one holding the write lock, so a document somebody
                # else touched between the preview and the button lands here.
                messages.error(request, str(exc))
            else:
                messages.success(
                    request, f"{document.code} cancelled; its entries have been reversed."
                )
                return redirect(back_url)

    context = {
        "document": document,
        "title": title,
        "back_url": back_url,
        "form": form,
        "blockers": blockers,
        "preview": preview_reversal(document),
        "chain": document.chain(),
        **(extra or {}),
    }
    status = 422 if request.method == "POST" else 200
    return render(request, CANCEL_TEMPLATE, context, status=status)


def shortcuts(request):
    """The keyboard reference at ``/shortcuts``.

    Rendered from :data:`apps.core.shortcuts.SHORTCUTS`, which is also what the
    JS binds — so this page cannot document a key that does nothing.

    No permission check: it describes keys, not data, and every one of them is
    inert on a screen the person cannot open.
    """
    from .shortcuts import SHORTCUTS

    return render(request, "core/shortcuts.html", {"shortcuts": SHORTCUTS})


__all__ = ["CANCEL_TEMPLATE", "cancel_view", "shortcuts"]
