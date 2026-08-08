"""The recovery workspace, and the screens around it.

The workspace is where the accountant lives. Everything about it is arranged
around one job: work down a list of shops that owe money, oldest first, and turn
each one into either a receipt or a note about why not.

Three rules govern every view here, the same three the sales entry screen
follows.

**The client never computes money.** Not an outstanding figure, not an ageing
bucket, not a total. Every one is computed by :mod:`apps.payments.recovery` on
the server and swapped in as already-rendered HTML.

**Views call services; they never write a ledger row.** Posting, cancelling,
clearing and bouncing are ``services.*``, each in its own
``transaction.atomic()`` (CLAUDE.md §4).

**Overdue money is in the alarm colour**, and so is a shop that has handed over
a cheque that bounced. Those are the two things somebody needs to see before
they pick up the phone.
"""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.accounting.enums import PartyType
from apps.core.enums import DocumentStatus
from apps.core.exceptions import CoreError
from apps.core.reporting import include_cancelled_from
from apps.core.views import cancel_view
from apps.masters.models import Client
from apps.reports.pdf import payment_receipt_pdf
from apps.reports.responses import pdf_filename, pdf_response, wants_download, wants_pdf

from . import recovery, services
from .enums import PaymentDirection, PaymentMode
from .forms import (
    AllocationForm,
    ChequeSettlementForm,
    InlineReceiptForm,
    PaymentForm,
    RecoveryFilterForm,
    field_name,
)
from .models import ChequeEvent, Payment, allocatable_model

#: How many autocomplete hits to show. Enough to find the shop, few enough to
#: scan without scrolling.
SEARCH_LIMIT = 8

#: How many rows of the sheet to render at once. A recovery round is worked in
#: route order and nobody scrolls past two hundred shops; the filters are how
#: you get to the rest.
ROW_LIMIT = 200


def _payment(pk: int) -> Payment:
    return get_object_or_404(
        Payment.objects.select_related("route", "collected_by").with_cheque_status(), pk=pk
    )


# ===========================================================================
# The recovery workspace
# ===========================================================================
def _workspace_context(request):
    """Everything the sheet shows, computed once for the page and its partials."""
    filters = RecoveryFilterForm(request.GET or None)
    criteria = filters.criteria()
    as_of = criteria["as_of"] or timezone.localdate()

    rows = recovery.recovery_rows(**criteria)
    day = recovery.todays_recovery(on=as_of, route=criteria["route"])
    collected, outstanding, payment_count = recovery.day_totals(day)

    return {
        "filters": filters,
        "criteria": criteria,
        "as_of": as_of,
        "rows": rows[:ROW_LIMIT],
        "row_count": len(rows),
        "truncated": max(len(rows) - ROW_LIMIT, 0),
        "summary": recovery.ageing_summary(rows),
        "total_open_paisa": sum(row.open_paisa for row in rows),
        "total_on_account_paisa": sum(row.on_account_paisa for row in rows),
        "total_overdue_paisa": sum(row.overdue_paisa for row in rows),
        "flagged_count": sum(1 for row in rows if row.is_flagged),
        "day_lines": day,
        "day_collected_paisa": collected,
        "day_outstanding_paisa": outstanding,
        "day_payment_count": payment_count,
    }


@login_required
@require_GET
def workspace(request):
    """The screen the accountant lives in."""
    return render(request, "payments/recovery.html", _workspace_context(request))


@login_required
@require_GET
def workspace_rows(request):
    """Just the sheet, for the filter bar to swap in without a page change."""
    return TemplateResponse(request, "payments/partials/sheet.html", _workspace_context(request))


def _row_context(request, client, *, receipt_form=None, allocation_form=None, as_of=None):
    as_of = as_of or _as_of(request)
    row = recovery.client_recovery(client, as_of=as_of)
    allocation_form = allocation_form or AllocationForm(open_items=row.open_items)
    return {
        "row": row,
        "client": client,
        "as_of": as_of,
        "receipt_form": receipt_form
        or InlineReceiptForm(
            initial={
                "posting_date": as_of,
                "mode": PaymentMode.CASH,
                "collected_by": client.seller_id,
            }
        ),
        "allocation_form": allocation_form,
        # Paired here rather than looked up in the template: a bound field per
        # open item, and Django templates cannot index a form by a computed key.
        "allocation_rows": [(item, allocation_form.field_for(item)) for item in row.open_items],
        "on_account": _on_account_payments(client, as_of),
    }


def _as_of(request):
    filters = RecoveryFilterForm(request.GET or None)
    return filters.criteria()["as_of"] or timezone.localdate()


def _on_account_payments(client, as_of):
    """This shop's live receipts with money still sitting on them.

    Shown in the expanded row because "they already paid, nobody applied it" is
    the single most common reason a shop is chased for money it does not owe.
    """
    payments = (
        Payment.objects.live()
        .filter(
            party_type=PartyType.CLIENT,
            party_id=client.pk,
            direction=PaymentDirection.RECEIVE,
            posting_date__lte=as_of,
        )
        .with_allocated()
        .order_by("posting_date", "id")
    )
    return [payment for payment in payments if payment.unallocated_paisa > 0]


@login_required
@require_GET
def client_row(request, pk: int):
    """The expanded row: this shop's open invoices, and the money on account."""
    client = get_object_or_404(Client.objects.select_related("route", "seller"), pk=pk)
    return TemplateResponse(
        request, "payments/partials/client_row.html", _row_context(request, client)
    )


@login_required
@require_POST
def client_receive(request, pk: int):
    """Take money from a shop and apply it, without leaving the sheet.

    One transaction's worth of work in one request: create the draft, post it,
    and allocate what the operator typed against the open invoices. If the
    allocation is refused the whole thing is refused — a posted receipt with a
    rejected allocation would leave money on account that nobody meant to leave
    there.
    """
    client = get_object_or_404(Client.objects.select_related("route", "seller"), pk=pk)
    as_of = _as_of(request)
    row = recovery.client_recovery(client, as_of=as_of)

    receipt_form = InlineReceiptForm(request.POST)
    allocation_form = AllocationForm(request.POST, open_items=row.open_items)
    valid = receipt_form.is_valid() & allocation_form.is_valid()

    if valid:
        try:
            payment = _take_money(request, client, receipt_form, allocation_form)
        except CoreError as exc:
            receipt_form.add_error(None, str(exc))
        else:
            messages.success(request, f"{payment.code} posted for {client.name}.")
            return TemplateResponse(
                request,
                "payments/partials/client_row.html",
                _row_context(request, client, as_of=as_of),
            )

    return TemplateResponse(
        request,
        "payments/partials/client_row.html",
        _row_context(
            request,
            client,
            receipt_form=receipt_form,
            allocation_form=allocation_form,
            as_of=as_of,
        ),
        status=422,
    )


@transaction.atomic
def _take_money(request, client, receipt_form, allocation_form):
    """Create, post and allocate — all three, or none of them.

    The ``atomic()`` here is doing real work and is not redundant with the one
    inside each service. Each of those commits on its own, so without this a
    rejected allocation would leave a **posted receipt** behind: money on the
    books that the operator was told had not been taken. Nested atomics are
    savepoints, so a refusal in the third step unwinds the first two.
    """
    payment = services.create_payment(
        party=client,
        direction=PaymentDirection.RECEIVE,
        created_by=request.user,
        updated_by=request.user,
        **receipt_form.payment_fields(),
    )
    services.post_payment(payment, user=request.user)

    allocations = [
        (allocatable_model(voucher_type).objects.get(pk=voucher_id), paisa)
        for (voucher_type, voucher_id), paisa in allocation_form.amounts().items()
    ]
    if allocations:
        services.allocate_payment(payment, allocations, user=request.user)
    return payment


@login_required
@require_POST
def client_allocate(request, pk: int):
    """Apply money already on account to this shop's open invoices.

    The other half of the inline action: the shop paid last week, nobody applied
    it, and the accountant is looking at the row that proves it.
    """
    client = get_object_or_404(Client.objects.select_related("route", "seller"), pk=pk)
    as_of = _as_of(request)
    payment = get_object_or_404(
        Payment.objects.live(),
        pk=request.POST.get("payment") or 0,
        party_type=PartyType.CLIENT,
        party_id=client.pk,
    )
    row = recovery.client_recovery(client, as_of=as_of)
    allocation_form = AllocationForm(request.POST, open_items=row.open_items)

    if allocation_form.is_valid():
        allocations = [
            (allocatable_model(voucher_type).objects.get(pk=voucher_id), paisa)
            for (voucher_type, voucher_id), paisa in allocation_form.amounts().items()
        ]
        try:
            services.allocate_payment(payment, allocations, replace=False, user=request.user)
        except CoreError as exc:
            allocation_form.add_error(None, str(exc))
        else:
            messages.success(request, f"{payment.code} applied.")
            return TemplateResponse(
                request,
                "payments/partials/client_row.html",
                _row_context(request, client, as_of=as_of),
            )

    return TemplateResponse(
        request,
        "payments/partials/client_row.html",
        _row_context(request, client, allocation_form=allocation_form, as_of=as_of),
        status=422,
    )


# ===========================================================================
# Payments — list, entry, lifecycle
# ===========================================================================
@login_required
@require_GET
def payment_list(request):
    """Every receipt and payment, filterable by the four things people ask about."""
    payments = Payment.objects.with_cheque_status().select_related("route", "collected_by")

    direction = request.GET.get("direction") or ""
    if direction in PaymentDirection.values:
        payments = payments.filter(direction=direction)

    mode = request.GET.get("mode") or ""
    if mode in PaymentMode.values:
        payments = payments.filter(mode=mode)

    status = request.GET.get("status") or ""
    if status in DocumentStatus.values:
        payments = payments.filter(status=status)

    query = (request.GET.get("q") or "").strip()
    if query:
        payments = payments.filter(Q(code__icontains=query) | Q(cheque_no__icontains=query))

    rows = services.attach_parties(payments.order_by("-posting_date", "-id")[:ROW_LIMIT])
    return render(
        request,
        "payments/document_list.html",
        {
            "payments": rows,
            "direction": direction,
            "mode": mode,
            "status": status,
            "query": query,
            "directions": PaymentDirection.choices,
            "modes": PaymentMode.choices,
            "statuses": DocumentStatus.choices,
        },
    )


@login_required
@require_GET
def cheque_register(request):
    """What is in the drawer, oldest cheque first.

    The total on this page is what account 1160 Cheques in Hand should be
    showing — a reconciliation somebody can do by eye, which is the point of
    keeping cheques out of Bank in the first place.

    A cancelled receipt is **not** in the drawer and is left out, because the
    total has to equal an account balance and a cancelled payment's entries have
    already been reversed out of it. ``?include_cancelled=1`` puts the cancelled
    cheques back on the page for somebody reconciling by hand; the total then
    says so rather than pretending to match 1160.
    """
    as_of = _as_of(request)
    include_cancelled = include_cancelled_from(request)
    cheques = services.attach_parties(
        recovery.pending_cheques(as_of=as_of, include_cancelled=include_cancelled)
    )
    live = [payment for payment in cheques if payment.status == DocumentStatus.POSTED]
    return render(
        request,
        "payments/cheque_register.html",
        {
            "cheques": cheques,
            "as_of": as_of,
            "include_cancelled": include_cancelled,
            # Summed over the live ones only, whichever rows are on screen: the
            # figure this page exists to reconcile is an account balance.
            "total_paisa": sum(payment.amount_paisa for payment in live),
            "cancelled_count": len(cheques) - len(live),
            "due_count": sum(1 for payment in live if payment.cheque_date <= as_of),
        },
    )


@login_required
def payment_create(request):
    """The header, then straight to the payment's own screen.

    The document code is allocated when this form is submitted, not when the
    page is opened — opening a screen and changing your mind must not burn a
    number out of the sequence (CLAUDE.md §5).
    """
    if request.method == "POST":
        form = PaymentForm(request.POST)
        if form.is_valid():
            try:
                payment = services.create_payment(
                    created_by=request.user,
                    updated_by=request.user,
                    **form.payment_fields(),
                )
            except CoreError as exc:
                form.add_error(None, str(exc))
            else:
                return redirect("payments:detail", pk=payment.pk)
    else:
        form = PaymentForm(
            initial={
                "posting_date": timezone.localdate(),
                "direction": PaymentDirection.RECEIVE,
                "mode": PaymentMode.CASH,
            }
        )

    return render(request, "payments/document_form.html", {"form": form})


@login_required
@require_GET
def payment_detail(request, pk: int):
    """One payment — or the receipt for it, with ``?format=pdf``.

    ``?layout=80mm`` picks the till roll for one job; without it the machine's
    own ``RECEIPT_LAYOUT`` setting decides, which is how the counter PC prints
    thermal and the back office prints A5 without either of them being told.
    """
    payment = _payment(pk)
    services.attach_parties([payment])

    if wants_pdf(request):
        return pdf_response(
            payment_receipt_pdf(payment, layout=request.GET.get("layout")),
            pdf_filename(payment.code, payment.party_name),
            download=wants_download(request),
        )

    # The shop's open bills, so the money can be applied without going back to
    # the sheet. Only for a client: a supplier payment is allocated from the
    # purchasing side, which does not exist as a screen yet.
    row = None
    allocation_form = None
    if payment.party_type == PartyType.CLIENT and payment.party is not None:
        row = recovery.client_recovery(payment.party, as_of=payment.posting_date)
        allocation_form = AllocationForm(
            open_items=row.open_items,
            initial=_current_allocations(payment),
        )

    return render(
        request,
        "payments/document_detail.html",
        {
            "payment": payment,
            "gl_lines": services.build_payment_gl(payment),
            "allocations": services.allocation_rows(payment),
            "cheque": services.cheque_summary(payment),
            "settlement_form": ChequeSettlementForm(initial={"posting_date": payment.cheque_date}),
            "allocation_form": allocation_form,
            "allocation_rows": [(item, allocation_form.field_for(item)) for item in row.open_items]
            if row
            else [],
            "row": row,
        },
    )


def _current_allocations(payment) -> dict:
    """What this payment already puts on each bill, as form initial data.

    So the screen opens showing the allocation as it stands rather than blank —
    ``allocate_payment`` replaces the whole set, and a blank form submitted by
    accident would silently take the money off every bill.
    """
    return {
        field_name(allocation.invoice_type, allocation.invoice_id): allocation.amount_paisa
        for allocation in payment.allocations.all()
    }


@login_required
@require_POST
def payment_post(request, pk: int):
    payment = _payment(pk)
    try:
        services.post_payment(payment, user=request.user)
    except CoreError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"{payment.code} posted.")
    return redirect("payments:detail", pk=payment.pk)


@login_required
def payment_cancel(request, pk: int):
    """The cancel screen: what would be reversed, then the button.

    The same screen the sales and purchase documents use — see
    :func:`apps.core.views.cancel_view`. A payment reverses two ledger rows and
    no stock, so the stock half of the preview is simply empty.
    """
    payment = _payment(pk)
    services.attach_parties([payment])
    return cancel_view(
        request,
        payment,
        cancel=services.cancel_payment,
        back_url=reverse("payments:detail", kwargs={"pk": payment.pk}),
        title=payment.get_direction_display(),
    )


@login_required
@require_POST
def payment_amend(request, pk: int):
    payment = _payment(pk)
    try:
        amendment = services.amend_payment(payment, user=request.user)
    except CoreError as exc:
        messages.error(request, str(exc))
        return redirect("payments:detail", pk=payment.pk)

    messages.success(request, f"{amendment.code} created from {payment.code}.")
    return redirect("payments:detail", pk=amendment.pk)


@login_required
@require_POST
def payment_delete(request, pk: int):
    """Delete a DRAFT. Anything that has touched the ledger refuses."""
    payment = _payment(pk)
    try:
        payment.delete()
    except CoreError as exc:
        messages.error(request, str(exc))
        return redirect("payments:detail", pk=payment.pk)

    messages.success(request, "Draft deleted.")
    return redirect("payments:list")


@login_required
@require_POST
def payment_allocate(request, pk: int):
    """Set which bills this payment settles, from its own screen."""
    payment = _payment(pk)
    party = payment.party
    if party is None or payment.party_type != PartyType.CLIENT:
        raise Http404("Only a client's payments are allocated from this screen.")

    row = recovery.client_recovery(party, as_of=payment.posting_date)
    form = AllocationForm(request.POST, open_items=row.open_items)

    if form.is_valid():
        allocations = [
            (allocatable_model(voucher_type).objects.get(pk=voucher_id), paisa)
            for (voucher_type, voucher_id), paisa in form.amounts().items()
        ]
        try:
            services.allocate_payment(payment, allocations, user=request.user)
        except CoreError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, f"{payment.code} allocated.")
    else:
        messages.error(request, "Check the amounts and try again.")

    return redirect("payments:detail", pk=payment.pk)


@login_required
@require_POST
def payment_auto_allocate(request, pk: int):
    """Spend the remainder on the oldest bills first."""
    payment = _payment(pk)
    try:
        services.auto_allocate(payment, user=request.user)
    except CoreError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"{payment.code} applied to the oldest bills.")
    return redirect("payments:detail", pk=payment.pk)


# ===========================================================================
# Cheques
# ===========================================================================
def _settle(request, pk: int, settle):
    """Shared body of the two settlement routes. Never chooses between them."""
    payment = _payment(pk)
    form = ChequeSettlementForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Check the date and try again.")
        return redirect("payments:detail", pk=payment.pk)

    try:
        event = settle(
            payment,
            posting_date=form.cleaned_data.get("posting_date") or None,
            remarks=form.cleaned_data.get("remarks") or "",
            user=request.user,
        )
    except CoreError as exc:
        messages.error(request, str(exc))
        return redirect("payments:detail", pk=payment.pk)
    return payment, event


@login_required
@require_POST
def cheque_clear(request, pk: int):
    """The bank took it: the money moves from the drawer into Bank."""
    result = _settle(request, pk, services.clear_cheque)
    if not isinstance(result, tuple):
        return result
    payment, event = result
    messages.success(request, f"{payment.code} cleared into the bank ({event.code}).")
    return redirect("payments:detail", pk=payment.pk)


@login_required
@require_POST
def cheque_bounce(request, pk: int):
    """The bank sent it back: the debt returns and the shop is flagged."""
    result = _settle(request, pk, services.bounce_cheque)
    if not isinstance(result, tuple):
        return result
    payment, event = result
    services.attach_parties([payment])
    messages.error(
        request,
        f"{payment.code} bounced ({event.code}). {payment.party_name} owes the money again "
        f"and is flagged on the recovery sheet.",
    )
    return redirect("payments:detail", pk=payment.pk)


@login_required
def cheque_event_cancel(request, pk: int):
    """Reverse a clearing or a bounce that was recorded in error.

    Through the same confirmation screen as everything else: what the reversal
    would write, a reason, and the ``payments.cancel_chequeevent`` permission. A
    cheque event has no page of its own, so both the way out and the way back
    are the payment's screen.
    """
    event = get_object_or_404(ChequeEvent.objects.select_related("payment"), pk=pk)
    return cancel_view(
        request,
        event,
        cancel=services.cancel_cheque_event,
        back_url=reverse("payments:detail", kwargs={"pk": event.payment_id}),
        title=f"{event.get_kind_display().lower()} cheque event",
    )


# ===========================================================================
# Autocomplete
# ===========================================================================
@login_required
@require_GET
def client_search(request):
    """Clients matching what has been typed, on **code, name or phone**.

    Each hit carries what they owe, because on this screen that is the next
    question and making the operator pick the shop to find out is a wasted
    keystroke.
    """
    query = (request.GET.get("q") or "").strip()
    results = []
    if query:
        clients = (
            Client.objects.filter(is_active=True)
            .filter(Q(code__icontains=query) | Q(name__icontains=query) | Q(phone__icontains=query))
            .select_related("route", "seller")
            .order_by("name")[:SEARCH_LIMIT]
        )
        for client in clients:
            results.append(
                {
                    "client": client,
                    "outstanding_paisa": recovery.party_open_total(PartyType.CLIENT, client.pk),
                }
            )
    return render(
        request, "payments/partials/client_results.html", {"results": results, "q": query}
    )
