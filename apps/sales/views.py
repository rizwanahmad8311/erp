"""The sales entry screens — the primary UI of this system.

This is the screen someone sits at all day. Everything about it is arranged
around that: no mouse, no page reloads, no waiting, and no arithmetic in the
browser.

Three rules govern every view here.

**The client never computes money.** Not the line amount, not the tax, not the
total, not the credit position, not the general ledger preview. Every one is
computed by :mod:`apps.sales.services` on the server and swapped in as
already-rendered HTML. There is JavaScript on this page, but it moves focus and
nothing else — see ``static/src/js/entry-grid.js``.

**Views call services; they never write a ledger row.** Posting, cancelling and
amending are ``services.post_*`` / ``cancel_*`` / ``amend_*``, each in its own
``transaction.atomic()`` (CLAUDE.md §4).

**The keyboard is the interface.** The tab order is fixed and documented in
:data:`TAB_ORDER`, which is rendered onto the screen itself as a help strip so
nobody has to guess.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.access import model_permission, require
from apps.accounts.scoping import scope_clients, scope_queryset, scoped_get_object_or_404
from apps.core.enums import DocumentStatus
from apps.core.exceptions import CoreError
from apps.core.views import cancel_view
from apps.masters.enums import Unit
from apps.masters.models import Client, Item
from apps.masters.services import fmt_qty
from apps.reports.pdf import sales_invoice_pdf
from apps.reports.responses import pdf_filename, pdf_response, wants_download, wants_pdf

from . import services
from .forms import LineEntryForm, SalesInvoiceForm, SalesReturnForm
from .models import SalesInvoice, SalesInvoiceLine, SalesReturn, SalesReturnLine

#: How many autocomplete hits to show. Enough to find the shop, few enough to
#: scan without scrolling — the operator refines by typing another letter.
SEARCH_LIMIT = 8

#: The documented keyboard route through the screen, rendered onto it as a help
#: strip. Every step is reachable with Tab, the arrow keys and Enter; nothing on
#: this screen needs a mouse.
TAB_ORDER = (
    ("1", "Item", "Type a code or name. ↓ ↑ to choose, Enter to pick."),
    ("2", "Qty", "Focus lands here the moment an item is picked."),
    ("3", "Unit", "Pre-set to CTN for a cartoned item, PCS otherwise."),
    ("4", "Rate", "Per the unit above. Pre-filled from the item's sale rate."),
    ("5", "Discount", "Optional. Tab past it if there is none."),
    ("6", "Enter", "Adds the line and puts you back on Item for the next one."),
    ("7", "Alt+P", "Posts the document once the lines are in."),
)


@dataclass(frozen=True)
class DocumentKind:
    """Everything that differs between the invoice screen and the credit note.

    One object rather than two view modules — the screens are the same screen,
    and only the model, the service and the words change.
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
    party_label: str
    #: Does this document take stock out (an invoice) or put it back (a note)?
    issues_stock: bool

    # -- who may do what to it -------------------------------------------
    # Derived from the model, never typed: these views serve two document types
    # off one set of URLs, and a hard-coded permission string here would guard
    # one of them with the other's.
    def permission(self, action: str) -> str:
        return model_permission(self.model, action)

    @property
    def view_permission(self) -> str:
        return self.permission("view")

    def assert_may(self, request, action: str) -> None:
        """Refuse this request unless it holds ``<app>.<action>_<model>``."""
        require(request.user, self.permission(action), doing=f"{self.title}s")

    def assert_may_use(self, request, permission: str, *, doing: str) -> None:
        """Refuse this request unless it holds a named permission."""
        require(request.user, permission, doing=doing)


INVOICE = DocumentKind(
    slug="invoices",
    model=SalesInvoice,
    line_model=SalesInvoiceLine,
    form_class=SalesInvoiceForm,
    create=services.create_sales_invoice,
    post=services.post_sales_invoice,
    cancel=services.cancel_sales_invoice,
    amend=services.amend_sales_invoice,
    title="Sales invoice",
    party_label="Client",
    issues_stock=True,
)

RETURN = DocumentKind(
    slug="returns",
    model=SalesReturn,
    line_model=SalesReturnLine,
    form_class=SalesReturnForm,
    create=services.create_sales_return,
    post=services.post_sales_return,
    cancel=services.cancel_sales_return,
    amend=services.amend_sales_return,
    title="Credit note",
    party_label="Client",
    issues_stock=False,
)

KINDS = {INVOICE.slug: INVOICE, RETURN.slug: RETURN}


def _kind(slug: str) -> DocumentKind:
    try:
        return KINDS[slug]
    except KeyError:
        raise Http404(f"No sales document type {slug!r}.") from None


def _get_document(request, kind: DocumentKind, pk: int):
    """One document, or a 404 — including for a document on somebody else's beat.

    Scoped rather than merely filtered out of the list: hiding a bill from a
    booker's screen and then serving it to anybody who types its id is not
    access control. A row outside the scope is a 404 rather than a 403 on
    purpose — see :mod:`apps.accounts.scoping`.
    """
    return scoped_get_object_or_404(
        kind.model.objects.select_related("client", "warehouse", "route", "seller"),
        request.user,
        pk=pk,
    )


def _editable(request, kind: DocumentKind, pk: int):
    """A document that may still be changed, or a 404-shaped refusal.

    A POSTED document's lines are frozen at the model layer too, but a screen
    that lets you type into them and then explodes on save is a screen that
    loses somebody's work.
    """
    document = _get_document(request, kind, pk)
    if not document.is_editable:
        raise Http404(f"{document.code} is {document.status} and can no longer be edited.")
    return document


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------
def _lines_for_display(kind: DocumentKind, document):
    """The saved lines, each carrying the rate as typed and the stock behind it.

    ``entry_rate_paisa`` divides the amount back out by ``qty_input``, which is
    exact — so a line entered as "4 cartons @ 3,000" is shown that way rather
    than as the derived per-piece figure nobody typed.

    ``available_base`` is read from the stock ledger per line rather than
    cached anywhere (CLAUDE.md §6). It is what stops somebody promising a
    shopkeeper twelve cartons that are not in the van.
    """
    rows = []
    for line in document.lines.select_related("item"):
        line.entry_rate_paisa = services.entry_rate_paisa(line)
        line.available_base = services.available_stock(document, line.item)
        line.available_display = fmt_qty(line.item, line.available_base)
        line.is_short = kind.issues_stock and line.qty_base > line.available_base
        rows.append(line)
    return rows


def _gl_preview(document):
    """The general ledger this document *would* post, from the posting service.

    The same functions that do the posting build this, so the preview cannot
    drift from what actually lands in the ledger.
    """
    if not document.lines.exists():
        return [], 0

    cogs = services.preview_cogs_paisa(document)
    if isinstance(document, SalesReturn):
        return services.build_return_gl(document, cogs_paisa=cogs), cogs
    return services.build_invoice_gl(document, cogs_paisa=cogs), cogs


def _entry_context(request, kind: DocumentKind, document, *, line_form=None):
    services.recalculate_totals(document, save=document.is_editable)
    gl_lines, cogs = _gl_preview(document)

    return {
        "kind": kind,
        "document": document,
        "lines": _lines_for_display(kind, document),
        "line_form": line_form if line_form is not None else LineEntryForm(),
        "gl_lines": gl_lines,
        "gl_debit_paisa": sum(line.debit_paisa for line in gl_lines),
        "gl_credit_paisa": sum(line.credit_paisa for line in gl_lines),
        "cogs_paisa": cogs,
        "credit": services.credit_status(document) if kind.issues_stock else None,
        "may_override": services.may_override_credit_limit(request.user),
        "is_return": not kind.issues_stock,
        "tab_order": TAB_ORDER,
    }


def _grid_response(request, kind: DocumentKind, document, *, line_form=None, status=200):
    """The HTMX response: lines, entry row and posting strip, in one swap.

    One response updates all three, so the totals can never be a step behind the
    lines they are the total of.
    """
    context = _entry_context(request, kind, document, line_form=line_form)
    return TemplateResponse(request, "sales/partials/grid.html", context, status=status)


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------
@login_required
@require_GET
def document_list(request, slug: str):
    kind = _kind(slug)
    kind.assert_may(request, "view")
    # Scoped at the view, never in the manager: the posting services and every
    # report must keep seeing everything (CLAUDE.md §6), so a screen that is
    # limited says so here rather than hiding it in a default queryset.
    documents = scope_queryset(
        kind.model.objects.select_related("client", "route", "seller", "warehouse"),
        request.user,
    )

    status = request.GET.get("status") or ""
    if status in DocumentStatus.values:
        documents = documents.filter(status=status)

    query = (request.GET.get("q") or "").strip()
    if query:
        documents = documents.filter(
            Q(code__icontains=query)
            | Q(client__name__icontains=query)
            | Q(client__code__icontains=query)
            | Q(client__phone__icontains=query)
        )

    return render(
        request,
        "sales/document_list.html",
        {
            "kind": kind,
            "documents": documents.order_by("-posting_date", "-id")[:200],
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
    kind.assert_may(request, "add")
    if request.method == "POST":
        form = kind.form_class(request.POST)
        if form.is_valid():
            fields = dict(form.cleaned_data)
            document = kind.create(
                client=fields.pop("client"),
                warehouse=fields.pop("warehouse"),
                posting_date=fields.pop("posting_date"),
                created_by=request.user,
                updated_by=request.user,
                **fields,
            )
            return redirect("sales:detail", slug=kind.slug, pk=document.pk)
    else:
        form = kind.form_class(initial={"posting_date": timezone.localdate()})

    return render(request, "sales/document_form.html", {"kind": kind, "form": form})


@login_required
@require_GET
def document_detail(request, slug: str, pk: int):
    """The entry screen — or the PDF of it, with ``?format=pdf``.

    Two output paths, on purpose (see :mod:`apps.reports.pdf`). The screen is
    the fast one: it carries a real ``@media print`` stylesheet, so Ctrl+P at
    the counter goes straight to the printer with no PDF step at all. The PDF is
    for the copy that has to be emailed or filed.

    Deliberately not inside any transaction: CLAUDE.md §4 says a posting
    transaction never does I/O, and this route only reads.
    """
    kind = _kind(slug)
    kind.assert_may(request, "view")
    document = _get_document(request, kind, pk)

    if wants_pdf(request):
        return pdf_response(
            sales_invoice_pdf(document, paper=request.GET.get("paper") or "a4"),
            pdf_filename(document.code, document.client.name),
            download=wants_download(request),
        )

    return render(request, "sales/document_detail.html", _entry_context(request, kind, document))


# ---------------------------------------------------------------------------
# Autocomplete
# ---------------------------------------------------------------------------
@login_required
@require_GET
def client_search(request):
    """Clients matching what has been typed, on **code, name or phone**.

    Phone is in there because that is how a shopkeeper on the line identifies
    themselves — they give a number, not a customer code.
    """
    query = (request.GET.get("q") or "").strip()
    clients = Client.objects.none()
    if query:
        clients = (
            scope_clients(Client.objects.filter(is_active=True), request.user)
            .filter(Q(code__icontains=query) | Q(name__icontains=query) | Q(phone__icontains=query))
            .select_related("route", "seller")
            .order_by("name")[:SEARCH_LIMIT]
        )
    return render(request, "sales/partials/client_results.html", {"clients": clients, "q": query})


@login_required
@require_GET
def item_search(request, slug: str, pk: int):
    """Items matching what has been typed, on **code or name**.

    Each hit carries what is on hand, because "is there any left" is the next
    question and making the operator pick the item to find out is a wasted
    keystroke on a screen that is used a thousand times a day.
    """
    kind = _kind(slug)
    kind.assert_may(request, "change")
    document = _get_document(request, kind, pk)
    query = (request.GET.get("q") or "").strip()

    items = []
    if query:
        found = (
            Item.objects.filter(is_active=True)
            .filter(Q(code__icontains=query) | Q(name__icontains=query))
            .order_by("code")[:SEARCH_LIMIT]
        )
        for item in found:
            available = services.available_stock(document, item)
            items.append(
                {
                    "item": item,
                    "available_base": available,
                    "available_display": fmt_qty(item, available),
                }
            )

    return render(
        request,
        "sales/partials/item_results.html",
        {"kind": kind, "document": document, "items": items, "q": query},
    )


@login_required
@require_GET
def item_pick(request, slug: str, pk: int, item_pk: int):
    """The entry row, filled in for the item that was just chosen.

    Everything the operator would otherwise have typed is set here, on the
    server:

    * the unit defaults to **CARTON when the item allows one**, PIECE otherwise;
    * the rate is pre-filled from the item's sale rate, converted to that unit —
      a per-carton rate for a per-carton entry, which is an exact integer
      multiplication and is done here rather than in the browser;
    * the stock on hand is shown beside it.

    The response carries ``data-autofocus`` on the quantity box, which is what
    makes Enter on the item list land the caret on Qty.
    """
    kind = _kind(slug)
    kind.assert_may(request, "change")
    document = _editable(request, kind, pk)
    item = get_object_or_404(Item, pk=item_pk, is_active=True)

    unit = Unit.CARTON if item.allows_carton else Unit.PIECE
    # Per base unit x pieces per carton. Two integers; nothing to round.
    rate_paisa = item.sale_rate_paisa * (item.carton_size if unit == Unit.CARTON else 1)

    form = LineEntryForm(
        initial={
            "item": item.pk,
            "unit_input": unit,
            "rate_input": rate_paisa,
            "qty_input": 1,
        }
    )
    available = services.available_stock(document, item)
    return render(
        request,
        "sales/partials/entry_row.html",
        {
            "kind": kind,
            "document": document,
            "line_form": form,
            "picked_item": item,
            "available_base": available,
            "available_display": fmt_qty(item, available),
            "autofocus": True,
        },
    )


# ---------------------------------------------------------------------------
# Lines — HTMX
# ---------------------------------------------------------------------------
@login_required
@require_POST
def line_add(request, slug: str, pk: int):
    """Add a line and hand back the whole grid, totals and credit position included."""
    kind = _kind(slug)
    kind.assert_may(request, "change")
    document = _editable(request, kind, pk)
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
    kind.assert_may(request, "change")
    document = _editable(request, kind, pk)
    get_object_or_404(kind.line_model, pk=line_pk, document=document).delete()
    return _grid_response(request, kind, document)


@login_required
@require_POST
def line_preview(request, slug: str, pk: int):
    """What the row being typed comes to — computed on the server, always.

    Fires on every change to the entry row. It saves nothing; it exists so the
    operator sees the amount, the tax and the base-unit quantity before
    committing the line, without the browser ever doing arithmetic on money.
    """
    kind = _kind(slug)
    kind.assert_may(request, "change")
    document = _editable(request, kind, pk)
    form = LineEntryForm(request.POST)

    preview = None
    error = None
    if form.is_valid():
        item = form.cleaned_data["item"]
        try:
            amounts = services.compute_line(
                item,
                qty_input=form.cleaned_data["qty_input"],
                unit_input=form.cleaned_data["unit_input"],
                rate_input_paisa=form.cleaned_data["rate_input"],
                discount_paisa=form.cleaned_data["discount"],
            )
            available = services.available_stock(document, item)
            preview = {
                "amounts": amounts,
                "item": item,
                "qty_display": fmt_qty(item, amounts.qty_base),
                "available_display": fmt_qty(item, available),
                "is_short": kind.issues_stock and amounts.qty_base > available,
            }
        except CoreError as exc:
            error = str(exc)

    return render(
        request,
        "sales/partials/line_preview.html",
        {"kind": kind, "document": document, "preview": preview, "error": error},
    )


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------
@login_required
@require_POST
def document_post(request, slug: str, pk: int):
    """Post it. The service does the work, atomically.

    The override checkbox is passed straight through — the service decides
    whether this user may actually use it, because a value off a form is not
    a permission.
    """
    kind = _kind(slug)
    kind.assert_may_use(request, kind.model.post_permission(), doing="Posting a sales document")
    document = _get_document(request, kind, pk)
    override = bool(request.POST.get("override_credit_limit"))

    try:
        if kind.issues_stock:
            kind.post(document, user=request.user, override_credit_limit=override)
        else:
            kind.post(document, user=request.user)
    except CoreError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"{document.code} posted.")
    return redirect("sales:detail", slug=kind.slug, pk=document.pk)


@login_required
def document_cancel(request, slug: str, pk: int):
    """The cancel screen: what would be reversed, then the button.

    GET shows the exact reversing entries and anything blocking; POST cancels.
    Both live in :func:`apps.core.views.cancel_view`, which owns the permission
    check, the reason and the preview — the three things this screen must not
    get right in only two apps out of three.
    """
    kind = _kind(slug)
    document = _get_document(request, kind, pk)
    return cancel_view(
        request,
        document,
        cancel=kind.cancel,
        back_url=reverse("sales:detail", kwargs={"slug": kind.slug, "pk": document.pk}),
        title=kind.title,
    )


@login_required
@require_POST
def document_amend(request, slug: str, pk: int):
    """Clone a cancelled document into a fresh draft and open it."""
    kind = _kind(slug)
    kind.assert_may_use(request, kind.model.amend_permission(), doing="Amending a sales document")
    document = _get_document(request, kind, pk)
    try:
        amendment = kind.amend(document, user=request.user)
    except CoreError as exc:
        messages.error(request, str(exc))
        return redirect("sales:detail", slug=kind.slug, pk=document.pk)

    messages.success(request, f"{amendment.code} created from {document.code}.")
    return redirect("sales:detail", slug=kind.slug, pk=amendment.pk)


@login_required
@require_POST
def document_delete(request, slug: str, pk: int):
    """Delete a DRAFT. Anything that has touched a ledger refuses."""
    kind = _kind(slug)
    kind.assert_may(request, "delete")
    document = _get_document(request, kind, pk)
    try:
        document.delete()
    except CoreError as exc:
        messages.error(request, str(exc))
        return redirect("sales:detail", slug=kind.slug, pk=document.pk)

    messages.success(request, "Draft deleted.")
    return redirect("sales:list", slug=kind.slug)
