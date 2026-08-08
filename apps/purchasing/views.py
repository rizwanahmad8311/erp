"""The purchase entry screens: a list, and a keyboard-driven grid.

Two rules govern every view here.

**The client never computes money.** Not the line amount, not the tax, not the
total, not the general ledger preview. Every one of those numbers is computed by
:mod:`apps.purchasing.services` on the server and swapped into the page as
already-rendered HTML. There is no JavaScript arithmetic on this screen and
there must not be — a browser doing paisa arithmetic in floats is CLAUDE.md §1
broken in the one place nobody thinks to look.

**Views call services; they never write a ledger row.** Posting, cancelling and
amending are ``services.post_*`` / ``cancel_*`` / ``amend_*``, each wrapped in
its own ``transaction.atomic()`` (CLAUDE.md §4).

The invoice and the return are the same screen with different labels and
different service functions, so they are one set of views parameterised by
:class:`DocumentKind` rather than two copies that drift apart.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Prefetch
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.core.enums import DocumentStatus
from apps.core.exceptions import CoreError
from apps.core.views import cancel_view
from apps.masters.services import fmt_qty
from apps.reports.pdf import purchase_invoice_pdf
from apps.reports.responses import pdf_filename, pdf_response, wants_download, wants_pdf

from . import services
from .forms import (
    LineEntryForm,
    PurchaseInvoiceForm,
    PurchaseReturnForm,
)
from .models import (
    PurchaseInvoice,
    PurchaseInvoiceLine,
    PurchaseReturn,
    PurchaseReturnLine,
)


@dataclass(frozen=True)
class DocumentKind:
    """Everything that differs between an invoice screen and a return screen.

    One object rather than two view modules. The screens are genuinely the same
    screen — the same grid, the same posting strip, the same keyboard — and the
    only things that change are the model, the service, and the words.
    """

    slug: str
    model: type
    line_model: type
    form_class: type
    create: Callable
    post: Callable
    cancel: Callable
    amend: Callable
    title: str
    bill_label: str
    party_label: str


INVOICE = DocumentKind(
    slug="invoices",
    model=PurchaseInvoice,
    line_model=PurchaseInvoiceLine,
    form_class=PurchaseInvoiceForm,
    create=services.create_purchase_invoice,
    post=services.post_purchase_invoice,
    cancel=services.cancel_purchase_invoice,
    amend=services.amend_purchase_invoice,
    title="Purchase invoice",
    bill_label="Supplier bill no.",
    party_label="Supplier",
)

RETURN = DocumentKind(
    slug="returns",
    model=PurchaseReturn,
    line_model=PurchaseReturnLine,
    form_class=PurchaseReturnForm,
    create=services.create_purchase_return,
    post=services.post_purchase_return,
    cancel=services.cancel_purchase_return,
    amend=services.amend_purchase_return,
    title="Purchase return",
    bill_label="Credit note no.",
    party_label="Supplier",
)

KINDS = {INVOICE.slug: INVOICE, RETURN.slug: RETURN}


def _kind(slug: str) -> DocumentKind:
    try:
        return KINDS[slug]
    except KeyError:
        raise Http404(f"No purchase document type {slug!r}.") from None


def _get_document(kind: DocumentKind, pk: int):
    return get_object_or_404(
        kind.model.objects.select_related("vendor", "warehouse"),
        pk=pk,
    )


def _editable(kind: DocumentKind, pk: int):
    """A document that may still be changed, or a 404-shaped refusal.

    Every line endpoint goes through this. A POSTED document's lines are frozen
    at the model layer too, but a screen that lets you type into them and then
    explodes on save is a screen that loses somebody's work.
    """
    document = _get_document(kind, pk)
    if not document.is_editable:
        raise Http404(f"{document.code} is {document.status} and can no longer be edited.")
    return document


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------
def _lines_with_entry_rate(document):
    """The saved lines, each carrying the rate as it was typed.

    ``entry_rate_paisa`` divides the amount back out by ``qty_input``, which is
    exact — so a line entered as "10 cartons @ 2,400" is shown that way rather
    than as the derived per-piece figure nobody typed.
    """
    rows = []
    for line in document.lines.select_related("item"):
        line.entry_rate_paisa = services.entry_rate_paisa(line)
        rows.append(line)
    return rows


def _gl_preview(document):
    """The general ledger this document *would* post, from the posting service.

    The same function that does the posting builds this. A preview computed any
    other way is a preview that will eventually disagree with what actually
    lands in the ledger, which is worse than no preview at all.
    """
    if not document.lines.exists():
        return [], None

    if isinstance(document, PurchaseReturn):
        cost = services.preview_return_cost_paisa(document)
        return services.build_return_gl(document, cost_released_paisa=cost), cost
    return services.build_invoice_gl(document), None


def _entry_context(request, kind: DocumentKind, document, *, line_form=None):
    services.recalculate_totals(document, save=document.is_editable)
    gl_lines, estimated_cost = _gl_preview(document)

    return {
        "kind": kind,
        "document": document,
        "lines": _lines_with_entry_rate(document),
        "line_form": line_form if line_form is not None else LineEntryForm(),
        "gl_lines": gl_lines,
        "gl_debit_paisa": sum(line.debit_paisa for line in gl_lines),
        "gl_credit_paisa": sum(line.credit_paisa for line in gl_lines),
        "estimated_cost_paisa": estimated_cost,
        "is_return": isinstance(document, PurchaseReturn),
    }


def _grid_response(request, kind: DocumentKind, document, *, line_form=None, status=200):
    """The HTMX response: the lines table, with the posting strip swapped too.

    One response updates both, so the totals can never be a step behind the
    lines they are the total of.
    """
    context = _entry_context(request, kind, document, line_form=line_form)
    return TemplateResponse(request, "purchasing/partials/grid.html", context, status=status)


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------
@login_required
@require_GET
def document_list(request, slug: str):
    kind = _kind(slug)
    documents = (
        kind.model.objects.select_related("vendor", "warehouse")
        .prefetch_related(Prefetch("lines", queryset=kind.line_model.objects.only("document_id")))
        .order_by("-posting_date", "-id")
    )

    status = request.GET.get("status") or ""
    if status in DocumentStatus.values:
        documents = documents.filter(status=status)

    query = (request.GET.get("q") or "").strip()
    if query:
        documents = (
            documents.filter(code__icontains=query)
            | documents.filter(vendor__name__icontains=query)
            | documents.filter(vendor_bill_no__icontains=query)
        )

    return render(
        request,
        "purchasing/document_list.html",
        {
            "kind": kind,
            "documents": documents[:200],
            "status": status,
            "query": query,
            "statuses": DocumentStatus.choices,
        },
    )


# ---------------------------------------------------------------------------
# Create and edit
# ---------------------------------------------------------------------------
@login_required
def document_create(request, slug: str):
    """The header, then straight into the grid.

    The document code is allocated when this form is submitted, not when the
    page is opened — opening a screen and changing your mind must not burn a
    number out of the sequence (CLAUDE.md §5).
    """
    kind = _kind(slug)
    if request.method == "POST":
        form = kind.form_class(request.POST)
        if form.is_valid():
            document = kind.create(
                vendor=form.cleaned_data["vendor"],
                warehouse=form.cleaned_data["warehouse"],
                posting_date=form.cleaned_data["posting_date"],
                vendor_bill_no=form.cleaned_data["vendor_bill_no"],
                vendor_bill_date=form.cleaned_data["vendor_bill_date"],
                remarks=form.cleaned_data["remarks"],
                created_by=request.user,
                updated_by=request.user,
            )
            return redirect("purchasing:detail", slug=kind.slug, pk=document.pk)
    else:
        form = kind.form_class(initial={"posting_date": timezone.localdate()})

    return render(request, "purchasing/document_form.html", {"kind": kind, "form": form})


@login_required
@require_GET
def document_detail(request, slug: str, pk: int):
    """The entry screen — or the PDF of it, with ``?format=pdf``.

    Two output paths (see :mod:`apps.reports.pdf`): the screen prints through
    the browser's own ``@media print`` stylesheet, which is the fast path, and
    the PDF is the copy that gets filed against the supplier's own bill.
    """
    kind = _kind(slug)
    document = _get_document(kind, pk)

    if wants_pdf(request):
        return pdf_response(
            purchase_invoice_pdf(document, paper=request.GET.get("paper") or "a4"),
            pdf_filename(document.code, document.vendor.name),
            download=wants_download(request),
        )

    context = _entry_context(request, kind, document)
    return render(request, "purchasing/document_detail.html", context)


# ---------------------------------------------------------------------------
# Lines — HTMX
# ---------------------------------------------------------------------------
@login_required
@require_POST
def line_add(request, slug: str, pk: int):
    """Add a line and hand back the whole grid, totals included.

    Bound to Enter on the entry row. The response replaces the lines table *and*
    the posting strip, so the two are always the same age.
    """
    kind = _kind(slug)
    document = _editable(kind, pk)
    form = LineEntryForm(request.POST)

    if not form.is_valid():
        return _grid_response(request, kind, document, line_form=form, status=422)

    try:
        line = services.update_line(
            kind.line_model(document=document),
            item=form.cleaned_data["item"],
            qty_input=form.cleaned_data["qty_input"],
            unit_input=form.cleaned_data["unit_input"],
            rate_input_paisa=form.cleaned_data["rate_input"],
            discount_paisa=form.cleaned_data["discount"],
        )
        line.save()
    except CoreError as exc:
        form.add_error(None, str(exc))
        return _grid_response(request, kind, document, line_form=form, status=422)

    return _grid_response(request, kind, document)


@login_required
@require_POST
def line_delete(request, slug: str, pk: int, line_pk: int):
    kind = _kind(slug)
    document = _editable(kind, pk)
    get_object_or_404(kind.line_model, pk=line_pk, document=document).delete()
    return _grid_response(request, kind, document)


@login_required
@require_POST
def line_preview(request, slug: str, pk: int):
    """What the row being typed comes to — computed on the server, always.

    Fires on every change to the entry row. It saves nothing; it exists so the
    operator can see the amount, the tax and the base-unit quantity before
    committing the line, without the browser ever doing arithmetic on money.
    """
    kind = _kind(slug)
    document = _editable(kind, pk)
    form = LineEntryForm(request.POST)

    preview = None
    error = None
    if form.is_valid():
        try:
            amounts = services.compute_line(
                form.cleaned_data["item"],
                qty_input=form.cleaned_data["qty_input"],
                unit_input=form.cleaned_data["unit_input"],
                rate_input_paisa=form.cleaned_data["rate_input"],
                discount_paisa=form.cleaned_data["discount"],
            )
            preview = {
                "amounts": amounts,
                "qty_display": fmt_qty(form.cleaned_data["item"], amounts.qty_base),
                "item": form.cleaned_data["item"],
            }
        except CoreError as exc:
            error = str(exc)

    return render(
        request,
        "purchasing/partials/line_preview.html",
        {"kind": kind, "document": document, "preview": preview, "error": error},
    )


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------
@login_required
@require_POST
def document_post(request, slug: str, pk: int):
    """Post it. The service does the work, atomically."""
    kind = _kind(slug)
    document = _get_document(kind, pk)
    try:
        kind.post(document, user=request.user)
    except CoreError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"{document.code} posted.")
    return redirect("purchasing:detail", slug=kind.slug, pk=document.pk)


@login_required
def document_cancel(request, slug: str, pk: int):
    """The cancel screen: what would be reversed, then the button.

    GET shows the exact reversing entries and anything blocking — an allocated
    payment, say; POST cancels. Both live in
    :func:`apps.core.views.cancel_view`, shared with sales and payments so the
    permission, the reason and the preview cannot differ between three screens.
    """
    kind = _kind(slug)
    document = _get_document(kind, pk)
    return cancel_view(
        request,
        document,
        cancel=kind.cancel,
        back_url=reverse("purchasing:detail", kwargs={"slug": kind.slug, "pk": document.pk}),
        title=kind.title,
    )


@login_required
@require_POST
def document_amend(request, slug: str, pk: int):
    """Clone a cancelled document into a fresh draft and open it."""
    kind = _kind(slug)
    document = _get_document(kind, pk)
    try:
        amendment = kind.amend(document, user=request.user)
    except CoreError as exc:
        messages.error(request, str(exc))
        return redirect("purchasing:detail", slug=kind.slug, pk=document.pk)

    messages.success(request, f"{amendment.code} created from {document.code}.")
    return redirect("purchasing:detail", slug=kind.slug, pk=amendment.pk)


@login_required
@require_POST
def document_delete(request, slug: str, pk: int):
    """Delete a DRAFT. Anything that has touched a ledger refuses."""
    kind = _kind(slug)
    document = _get_document(kind, pk)
    try:
        document.delete()
    except CoreError as exc:
        messages.error(request, str(exc))
        return redirect("purchasing:detail", slug=kind.slug, pk=document.pk)

    messages.success(request, "Draft deleted.")
    return redirect("purchasing:list", slug=kind.slug)
