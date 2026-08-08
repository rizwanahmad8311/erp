"""Money in and money out, what it was applied to, and what the bank did with
the cheque.

Three models, and the split between them is the design:

:class:`Payment`
    A document (CLAUDE.md §5): DRAFT, then POSTED, then possibly CANCELLED. It
    records that money physically moved between us and a party on a day, in
    cash, by bank transfer, or as a piece of paper somebody will take to a bank
    later.

:class:`PaymentAllocation`
    Which bills that money settles. Deliberately **not** part of the posting: a
    receipt debits Cash and credits Accounts Receivable whether or not anybody
    has yet decided which invoices it clears, and in this business the money
    usually arrives first. Allocations therefore stay editable after the payment
    is posted, which is the whole point of the recovery workspace.

:class:`ChequeEvent`
    A document of its own, for what happened next. A cheque taken today and
    banked in three weeks is two events on two dates, and a system that folds
    them into one either shows money in the bank that is not there or loses the
    date it actually arrived.

Three rules hold across all of them.

**Nothing here caches a balance.** There is no ``paid_paisa`` on an invoice, no
``outstanding`` on a client, and no ``cheque_status`` column on a payment
(CLAUDE.md §6). Every one of those is aggregated — from the ledger, from the
allocation rows, or from the cheque events — by :mod:`apps.payments.services`
and :mod:`apps.payments.recovery`.

**The party is a soft link**, a ``(type, id)`` pair with no foreign key, exactly
as :class:`~apps.accounting.models.LedgerEntry` carries it. A payment and the
ledger rows it writes have to agree about who the money was with, and the
cheapest way to guarantee that is for both to say it the same way.

**The document a payment settles is a soft link too.** Money is applied to sales
invoices, purchase invoices and credit notes, which are four models in two apps
today and more later. A foreign key per type would mean four nullable columns
and a CHECK constraint that exactly one of them is set.
"""

from __future__ import annotations

from typing import NamedTuple

from django.apps import apps as django_apps
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Exists, OuterRef, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from django.urls import reverse

from apps.accounting.enums import PartyType
from apps.accounting.refs import PartyRef
from apps.core.enums import DocumentStatus
from apps.core.fields import MoneyField
from apps.core.lifecycle import Dependent
from apps.core.models import DocumentModel, TimeStampedModel
from apps.core.reporting import DocumentQuerySet

from .enums import ChequeEventKind, ChequeStatus, PaymentDirection, PaymentMode
from .exceptions import ChequeSettled, InvalidPayment, NotAllocatable

#: Where a party of each type is looked up. A soft link needs somewhere to
#: resolve, and one mapping is better than ``if party_type == ...`` in six views.
PARTY_MODELS: dict[str, tuple[str, str]] = {
    PartyType.CLIENT: ("masters", "Client"),
    PartyType.VENDOR: ("masters", "Vendor"),
}


class Allocatable(NamedTuple):
    """Everything payments needs to know about a document it can settle.

    Four facts, and payments knows nothing else about sales or purchasing. The
    party fields are here because an allocation has to check that the money and
    the bill belong to the same shop, and ``due_date_field`` because ageing is
    measured from when a bill fell due and only the document knows that.
    """

    app_label: str
    model_name: str
    party_type: str
    #: Attribute holding the party's row id — ``client_id`` or ``vendor_id``.
    party_field: str
    #: Field holding the day it falls due, or ``None`` when it is due on sight.
    due_date_field: str | None


#: The document types money may be allocated against, keyed by the same name
#: :class:`~apps.accounting.refs.VoucherRef` uses — the model's class name.
#:
#: A register rather than an import: payments must not depend on sales and
#: purchasing at module scope, and a type that is not listed here is refused
#: outright rather than stored as a link nothing can resolve.
ALLOCATABLE_DOCUMENTS: dict[str, Allocatable] = {
    "SalesInvoice": Allocatable("sales", "SalesInvoice", PartyType.CLIENT, "client_id", "due_date"),
    "SalesReturn": Allocatable("sales", "SalesReturn", PartyType.CLIENT, "client_id", None),
    "PurchaseInvoice": Allocatable(
        "purchasing", "PurchaseInvoice", PartyType.VENDOR, "vendor_id", None
    ),
    "PurchaseReturn": Allocatable(
        "purchasing", "PurchaseReturn", PartyType.VENDOR, "vendor_id", None
    ),
}


def party_model(party_type: str):
    """The master model a party type resolves to."""
    try:
        app_label, model_name = PARTY_MODELS[party_type]
    except KeyError:
        raise InvalidPayment(
            f"Unknown party type {party_type!r}; expected one of {sorted(PARTY_MODELS)}."
        ) from None
    return django_apps.get_model(app_label, model_name)


def party_type_of(party) -> str:
    """``CLIENT`` or ``VENDOR``, from a master instance.

    Matched on the model rather than on a ``type`` attribute the master does not
    have. Clients and vendors are separate tables on purpose — see
    :class:`apps.masters.models.Party` — and this is where that pays off.
    """
    for value, (app_label, model_name) in PARTY_MODELS.items():
        if isinstance(party, django_apps.get_model(app_label, model_name)):
            return value
    raise InvalidPayment(
        f"{type(party).__name__} is not a party money can move with; expected a Client or a Vendor."
    )


def allocatable_spec(invoice_type: str) -> Allocatable:
    """What payments knows about a document type, or a refusal naming it."""
    try:
        return ALLOCATABLE_DOCUMENTS[invoice_type]
    except KeyError:
        raise NotAllocatable(
            f"{invoice_type} is not a document money can be allocated to; expected one of "
            f"{', '.join(sorted(ALLOCATABLE_DOCUMENTS))}."
        ) from None


def allocatable_model(invoice_type: str):
    """The document model an allocation's ``invoice_type`` resolves to."""
    spec = allocatable_spec(invoice_type)
    return django_apps.get_model(spec.app_label, spec.model_name)


# ===========================================================================
# Payment
# ===========================================================================
class PaymentQuerySet(DocumentQuerySet):
    """The two questions every payments query starts with.

    Built on :class:`~apps.core.reporting.DocumentQuerySet`, so a payment answers
    ``cancelled()`` and ``for_report()`` like every other document. It narrows
    ``live()``, because for money "not cancelled" is not enough — see below.
    """

    def live(self):
        """Payments whose money is really there: POSTED, and not bounced.

        Narrower than the base ``live()``, which only excludes CANCELLED. A
        bounced cheque leaves its payment POSTED — it is a true record of a
        cheque that was taken on a day — but the money never arrived, so the
        allocations it carries must stop settling anything. Filtering here
        rather than in each caller is what stops one report counting a bounced
        cheque as recovery.
        """
        bounced = ChequeEvent.objects.filter(
            payment_id=OuterRef("pk"),
            status=DocumentStatus.POSTED,
            kind=ChequeEventKind.BOUNCED,
        )
        return self.filter(status=DocumentStatus.POSTED).exclude(Exists(bounced))

    def with_cheque_status(self):
        """Annotate ``_cheque_status`` so a list of cheques is one query.

        :attr:`Payment.cheque_status` reads the annotation when it is there and
        falls back to a query when it is not, the same way
        :attr:`apps.masters.models.Route.client_count` does — so the property is
        always right, and a changelist of two hundred cheques is not two hundred
        queries.
        """
        settled = ChequeEvent.objects.filter(
            payment_id=OuterRef("pk"), status=DocumentStatus.POSTED
        ).order_by("id")
        return self.annotate(
            _cheque_status=Coalesce(
                Subquery(settled.values("kind")[:1]),
                Value(ChequeStatus.PENDING),
                output_field=models.CharField(),
            )
        )

    def with_allocated(self):
        """Annotate ``_allocated_paisa``, the sum of this payment's allocations."""
        return self.annotate(_allocated_paisa=Coalesce(Sum("allocations__amount_paisa"), Value(0)))


class Payment(DocumentModel):
    """Money moved between us and one party, on one day, in one form.

    Posting writes exactly two ledger rows (see
    :func:`apps.payments.services.post_payment`):

        RECEIVE   Dr Cash / Bank / Cheques in Hand,  Cr the party account
        PAY       Dr the party account,              Cr Cash / Bank / Cheques Issued

    The party account is Accounts Receivable for a client and Accounts Payable
    for a vendor, **always** — the direction decides which side it goes on, not
    which account it is. That is what lets a vendor refund and a customer refund
    be ordinary postings rather than special cases.

    ``collected_by`` and ``route`` are what make recovery reportable. The seller
    is who physically took the money, which is not necessarily the shop's usual
    booker, and the route is the beat the collection belongs to. Both are
    recorded on the payment rather than read back through the client, so a shop
    that changes beat next year does not rewrite last year's recovery sheet.
    """

    objects = PaymentQuerySet.as_manager()

    # -- who -----------------------------------------------------------------
    # A soft (type, id) pair rather than two nullable foreign keys, matching
    # LedgerEntry. See the module docstring.
    party_type = models.CharField(
        max_length=8,
        choices=PartyType.choices,
        db_index=True,
        help_text="CLIENT or VENDOR. The party account follows from this, not from direction.",
    )
    party_id = models.BigIntegerField(
        help_text="Soft link to masters, exactly as the ledger carries it.",
    )

    # -- what ----------------------------------------------------------------
    direction = models.CharField(
        max_length=8,
        choices=PaymentDirection.choices,
        db_index=True,
        help_text="Which way the money went. Independent of the party type.",
    )
    mode = models.CharField(
        max_length=8,
        choices=PaymentMode.choices,
        db_index=True,
        help_text="Cash, bank transfer, or cheque. Decides which account the money lands in.",
    )
    posting_date = models.DateField(
        db_index=True,
        help_text="The day the money changed hands. Not the day it was typed in.",
    )
    amount_paisa = MoneyField(
        non_negative=True,
        help_text="What moved, in paisa. Always positive — direction carries the sign.",
    )

    # -- the cheque, when there is one --------------------------------------
    cheque_no = models.CharField(
        max_length=32,
        blank=True,
        default="",
        db_index=True,
        help_text="The number on the cheque. Required when mode is CHEQUE, empty otherwise.",
    )
    cheque_date = models.DateField(
        null=True,
        blank=True,
        db_index=True,
        help_text=(
            "The date written on the cheque, which for a post-dated cheque is the "
            "earliest it can be banked. Not the day it was taken."
        ),
    )
    bank_name = models.CharField(
        max_length=128,
        blank=True,
        default="",
        help_text="The bank the cheque is drawn on. Optional even on a cheque.",
    )

    # -- recovery ------------------------------------------------------------
    collected_by = models.ForeignKey(
        "masters.Seller",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="payments_collected",
        help_text="Who physically collected it. PROTECT: a seller with collections stays.",
    )
    route = models.ForeignKey(
        "masters.Route",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="payments",
        help_text="The beat this collection belongs to. Recovery is reported per route.",
    )

    remarks = models.TextField(blank=True, default="")

    class Meta:
        verbose_name = "payment"
        verbose_name_plural = "payments"
        ordering = ["-posting_date", "-id"]
        permissions = [
            ("post_payment", "Can post a receipt or payment to the ledger"),
            ("cancel_payment", "Can cancel a receipt or payment and reverse its entries"),
            ("amend_payment", "Can raise an amendment of a cancelled receipt or payment"),
        ]
        indexes = [
            # Every party statement, ageing row and recovery figure starts here.
            models.Index(fields=["party_type", "party_id"], name="payment_party_idx"),
            # "What did this route collect today", which is the workspace's
            # second panel.
            models.Index(fields=["posting_date", "route"], name="payment_date_route_idx"),
            # The cheque register: what is in the drawer, oldest first.
            models.Index(fields=["mode", "cheque_date"], name="payment_cheque_idx"),
        ]
        constraints = [
            # A payment that moves nothing is not a payment. It would sit in the
            # day book claiming something happened and contribute nothing.
            models.CheckConstraint(
                name="payment_amount_is_positive",
                condition=models.Q(amount_paisa__gt=0),
                violation_error_message="A payment moves a positive amount.",
            ),
            # The cheque columns and the mode agree, or a cheque with no number
            # is unfindable in the drawer and a cash receipt claims to be one.
            models.CheckConstraint(
                name="payment_cheque_fields_match_mode",
                condition=(
                    (
                        models.Q(mode=PaymentMode.CHEQUE, cheque_date__isnull=False)
                        & ~models.Q(cheque_no="")
                    )
                    | (
                        ~models.Q(mode=PaymentMode.CHEQUE)
                        & models.Q(cheque_no="", cheque_date__isnull=True, bank_name="")
                    )
                ),
                violation_error_message=(
                    "A cheque carries a number and a date; nothing else carries either."
                ),
            ),
        ]

    def __str__(self) -> str:
        return f"{self.code} — {self.get_direction_display()} {self.amount_paisa} paisa"

    def get_absolute_url(self) -> str:
        return reverse("payments:detail", kwargs={"pk": self.pk})

    # ------------------------------------------------------------------
    # What blocks a cancellation
    # ------------------------------------------------------------------
    def dependents(self) -> list[Dependent]:
        """A cheque event the bank has already caused, and nothing else.

        The allocations are deliberately **not** blockers, unlike on an invoice.
        An allocation writes no ledger row (see :class:`PaymentAllocation`), and
        :meth:`PaymentQuerySet.live` already drops a cancelled payment out of
        every figure — so the bills it was paying go back to being open the
        moment it is cancelled, with nothing to unpick first.

        A settled cheque is a different matter: it is a second document with its
        own entries hanging off this one, and reversing this one alone would
        leave those entries pointing at nothing.
        """
        if self.pk is None:
            return []  # nothing can point at a row that has never been saved
        settled = self.settled_event()
        if settled is None:
            return []
        return [
            Dependent(
                kind="cheque event",
                code=settled.code,
                detail=(
                    f"the cheque {settled.get_kind_display().lower()} on {settled.posting_date}"
                ),
                action=f"Cancel {settled.code} first, which reverses its entries, then cancel this.",
                document=settled,
                error=ChequeSettled,
            )
        ]

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    def _assert_shape(self) -> None:
        """The CHECK constraints, raised in Python first so they read as English.

        The constraints are the real guarantee; this exists so a mistake fails
        with a sentence rather than with an ``IntegrityError`` naming an index.
        """
        if isinstance(self.amount_paisa, bool) or not isinstance(self.amount_paisa, int):
            raise InvalidPayment(
                f"amount_paisa must be whole paisa as an int, got "
                f"{type(self.amount_paisa).__name__}: {self.amount_paisa!r}"
            )
        if self.amount_paisa <= 0:
            raise InvalidPayment(
                f"{type(self).__name__} amount is {self.amount_paisa} paisa. A payment moves a "
                f"positive amount — the direction carries the sign, not the number."
            )

        if self.party_type not in PARTY_MODELS:
            raise InvalidPayment(
                f"Unknown party type {self.party_type!r}; expected one of {sorted(PARTY_MODELS)}."
            )

        if self.is_cheque:
            if not (self.cheque_no or "").strip():
                raise InvalidPayment(
                    "A cheque payment needs the cheque number. It is what finds the piece of "
                    "paper in the drawer when the bank asks about it."
                )
            if self.cheque_date is None:
                raise InvalidPayment(
                    "A cheque payment needs the date written on the cheque. On a post-dated "
                    "cheque that is the earliest day it can be banked, which is the whole "
                    "reason it is being tracked."
                )
        elif self.cheque_no or self.cheque_date or self.bank_name:
            raise InvalidPayment(
                f"A {self.get_mode_display().lower()} payment carries cheque details "
                f"({self.cheque_no or self.bank_name or self.cheque_date}). Set the mode to "
                f"CHEQUE, or clear them."
            )

    def clean(self):
        """Surface :meth:`_assert_shape` as a form error in the admin."""
        super().clean()
        try:
            self._assert_shape()
        except InvalidPayment as exc:
            raise ValidationError(str(exc)) from exc

    def save(self, *args, **kwargs):
        self._assert_shape()
        return super().save(*args, **kwargs)

    # ------------------------------------------------------------------
    # The party
    # ------------------------------------------------------------------
    @property
    def party_ref(self) -> PartyRef:
        """The pair the ledger wants, validated on the way out."""
        return PartyRef(self.party_type, self.party_id)

    @property
    def party(self):
        """The master this payment was with, fetched on demand.

        Cached on the instance after the first read, so a template that names
        the party four times is still one query. There is no ``select_related``
        for a soft link — :func:`apps.payments.services.attach_parties` bulk
        loads a page of them.
        """
        cached = getattr(self, "_party", None)
        if cached is None:
            cached = party_model(self.party_type).objects.filter(pk=self.party_id).first()
            self._party = cached
        return cached

    @property
    def party_name(self) -> str:
        party = self.party
        return party.name if party is not None else f"{self.party_type}#{self.party_id}"

    def apply_party_defaults(self, party=None) -> None:
        """Fill the route and the collector in from the client, if they are blank.

        Only fills what is empty, so an override survives a re-save — the same
        rule as :meth:`apps.sales.models.SalesDocument.apply_client_defaults`,
        and for the same reason: a booker covering somebody else's beat records
        that at entry time, and next year's route change must not rewrite this
        year's recovery sheet.

        Vendors have neither field, so paying a supplier leaves both blank.
        """
        party = party if party is not None else self.party
        if party is None:
            return
        if self.route_id is None:
            self.route_id = getattr(party, "route_id", None)
        if self.collected_by_id is None:
            self.collected_by_id = getattr(party, "seller_id", None)

    # ------------------------------------------------------------------
    # The cheque
    # ------------------------------------------------------------------
    @property
    def is_cheque(self) -> bool:
        return self.mode == PaymentMode.CHEQUE

    @property
    def cheque_status(self) -> str | None:
        """PENDING, CLEARED or BOUNCED — **derived from the cheque events**.

        ``None`` for anything that is not a cheque. There is no column behind
        this and there must not be: a POSTED document is immutable (CLAUDE.md
        §5), so writing a status onto one would mean either weakening that guard
        or keeping a second copy of the truth that can disagree with the events
        (CLAUDE.md §6). The events are the truth; this reads them.
        """
        if not self.is_cheque:
            return None
        annotated = getattr(self, "_cheque_status", None)
        if annotated is not None:
            return annotated
        kind = (
            self.cheque_events.filter(status=DocumentStatus.POSTED)
            .values_list("kind", flat=True)
            .first()
        )
        return kind or ChequeStatus.PENDING

    @property
    def cheque_status_label(self) -> str:
        status = self.cheque_status
        return ChequeStatus(status).label if status else ""

    @property
    def is_bounced(self) -> bool:
        return self.cheque_status == ChequeStatus.BOUNCED

    @property
    def is_cleared(self) -> bool:
        return self.cheque_status == ChequeStatus.CLEARED

    @property
    def is_pending_cheque(self) -> bool:
        """A cheque that is posted and still sitting in the drawer."""
        return self.status == DocumentStatus.POSTED and self.cheque_status == ChequeStatus.PENDING

    @property
    def is_live(self) -> bool:
        """Whether this payment's money is really there.

        POSTED and not bounced. The Python twin of
        :meth:`PaymentQuerySet.live`, for when you are holding one instance
        rather than a queryset.
        """
        return self.status == DocumentStatus.POSTED and not self.is_bounced

    def settled_event(self):
        """The POSTED cheque event, if the bank has done something. Else ``None``."""
        return self.cheque_events.filter(status=DocumentStatus.POSTED).first()

    # ------------------------------------------------------------------
    # Allocation. Derived, never stored (CLAUDE.md §6).
    # ------------------------------------------------------------------
    @property
    def allocated_paisa(self) -> int:
        """How much of this payment has been applied to specific documents."""
        annotated = getattr(self, "_allocated_paisa", None)
        if annotated is not None:
            return annotated
        return self.allocations.aggregate(
            total=Coalesce(Sum("amount_paisa"), Value(0)),
        )["total"]

    @property
    def unallocated_paisa(self) -> int:
        """Money received that has not been applied to a bill yet.

        **This is not an error state.** A shopkeeper hands over Rs 20,000
        against four invoices nobody has looked up yet, and the money is on
        account until somebody does. What the workspace must never do is hide
        it — an on-account balance that is invisible is money that gets
        collected twice.
        """
        return self.amount_paisa - self.allocated_paisa

    @property
    def is_fully_allocated(self) -> bool:
        return self.unallocated_paisa == 0

    # ------------------------------------------------------------------
    # Lifecycle. The work is in services; these are the entry points.
    # ------------------------------------------------------------------
    def post(self, *, user=None):
        from .services import post_payment

        return post_payment(self, user=user)

    def cancel(self, *, user=None, reason: str = ""):
        from .services import cancel_payment

        return cancel_payment(self, user=user, reason=reason)

    def amend(self, *, user=None):
        from .services import amend_payment

        return amend_payment(self, user=user)


# ===========================================================================
# Allocation
# ===========================================================================
class PaymentAllocationQuerySet(models.QuerySet):
    def live(self):
        """Allocations whose money is really there.

        The mirror of :meth:`PaymentQuerySet.live`, expressed against the
        allocation so that "what has been paid on this invoice" is one query
        rather than a join the caller has to remember to add.
        """
        bounced = ChequeEvent.objects.filter(
            payment_id=OuterRef("payment_id"),
            status=DocumentStatus.POSTED,
            kind=ChequeEventKind.BOUNCED,
        )
        return self.filter(payment__status=DocumentStatus.POSTED).exclude(Exists(bounced))

    def for_document(self, document):
        return self.filter(invoice_type=type(document).__name__, invoice_id=document.pk)


class PaymentAllocation(TimeStampedModel):
    """How much of one payment settles one document.

    A **link, not a posting.** Nothing here writes a ledger row, and that is
    what makes it editable after the payment is posted: the receipt already
    debited Cash and credited the client's receivable, and moving Rs 5,000 of it
    from one invoice to another changes nothing the general ledger has to say.
    It changes which bills the recovery sheet shows as open, which is the
    accountant's job and not the ledger's.

    Because it is a link rather than history, it may be rewritten — see
    :func:`apps.payments.services.allocate_payment`, which replaces a payment's
    whole set in one transaction. The append-only rule in CLAUDE.md §3 is about
    :class:`~apps.accounting.models.LedgerEntry` and
    :class:`~apps.accounting.models.StockEntry`, and this is neither.

    The document is a soft ``(type, id)`` pair for the same reason the ledger's
    voucher link is: money is applied to four document models in two apps today
    and more later, and four nullable foreign keys with a
    exactly-one-is-set constraint is not a better version of this.
    """

    objects = PaymentAllocationQuerySet.as_manager()

    payment = models.ForeignKey(
        Payment,
        on_delete=models.CASCADE,
        related_name="allocations",
        help_text="CASCADE: an allocation has no meaning without its payment.",
    )

    invoice_type = models.CharField(
        max_length=64,
        help_text='The document model name, e.g. "SalesInvoice". See ALLOCATABLE_DOCUMENTS.',
    )
    invoice_id = models.BigIntegerField(
        help_text="Soft link to the document. Deliberately not a foreign key.",
    )

    amount_paisa = MoneyField(
        non_negative=True,
        help_text="How much of the payment lands on this document, in paisa.",
    )

    class Meta:
        verbose_name = "payment allocation"
        verbose_name_plural = "payment allocations"
        ordering = ["payment_id", "id"]
        indexes = [
            # "What has been paid against this invoice" — the seam purchasing
            # and sales read through payment_allocations().
            models.Index(fields=["invoice_type", "invoice_id"], name="allocation_document_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                name="paymentallocation_amount_is_positive",
                condition=models.Q(amount_paisa__gt=0),
                violation_error_message="An allocation applies a positive amount.",
            ),
            # One row per (payment, document). Two rows for the same pair are
            # two answers to "how much of this payment is on that invoice", and
            # every sum in the system would quietly add them together.
            models.UniqueConstraint(
                fields=["payment", "invoice_type", "invoice_id"],
                name="paymentallocation_one_row_per_document",
                violation_error_message="That payment is already allocated to this document.",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.payment_id} -> {self.invoice_type}#{self.invoice_id}: {self.amount_paisa}"

    @property
    def document(self):
        """The document this money settles, fetched on demand."""
        cached = getattr(self, "_document", None)
        if cached is None:
            cached = allocatable_model(self.invoice_type).objects.filter(pk=self.invoice_id).first()
            self._document = cached
        return cached

    @property
    def document_code(self) -> str:
        document = self.document
        return document.code if document is not None else f"{self.invoice_type}#{self.invoice_id}"


# ===========================================================================
# Cheque events
# ===========================================================================
class ChequeEvent(DocumentModel):
    """What the bank did with a cheque: a posting of its own, on its own date.

    Clearing is a separate action from taking the cheque because it happens on a
    separate day, and usually weeks later. Until it happens the money is in
    Cheques in Hand, not in Bank, and the difference between those two figures
    is the only honest answer to "how much have we actually got".

        CLEARED, RECEIVE   Dr Bank              Cr Cheques in Hand
        CLEARED, PAY       Dr Cheques Issued    Cr Bank
        BOUNCED, RECEIVE   Dr Accounts Receivable (party)  Cr Cheques in Hand
        BOUNCED, PAY       Dr Cheques Issued    Cr Accounts Payable (party)

    A bounce writes the payment's own posting back the other way, which puts the
    receivable exactly where it was and empties Cheques in Hand of a cheque that
    turned out to be nothing. It does **not** touch the payment: the payment is
    a true record that a cheque was taken on a day, and CLAUDE.md §5 leaves no
    room for editing a POSTED document anyway. What the bounce does do is stop
    the payment settling anything — :meth:`PaymentQuerySet.live` excludes it, so
    every invoice it was allocated to goes back to being open, and the client's
    balance goes back up.

    Recorded a bounce by mistake? Cancel the event. That writes true reversing
    rows (CLAUDE.md §3) and the payment goes back to being good money.

    At most one POSTED event per payment, enforced by a partial unique index: a
    cheque clears or it bounces, once. A draft second event is allowed, so
    somebody can start recording the bounce of a cheque whose clearing was
    entered in error before they cancel the clearing.
    """

    payment = models.ForeignKey(
        Payment,
        on_delete=models.PROTECT,
        related_name="cheque_events",
        help_text="PROTECT: a payment whose cheque has been settled cannot be deleted.",
    )
    kind = models.CharField(
        max_length=8,
        choices=ChequeEventKind.choices,
        db_index=True,
        help_text="What the bank did. Both are postings, not flags.",
    )
    posting_date = models.DateField(
        db_index=True,
        help_text="The day the bank did it. Not the day the cheque was taken.",
    )
    remarks = models.TextField(blank=True, default="")

    class Meta:
        verbose_name = "cheque event"
        verbose_name_plural = "cheque events"
        ordering = ["-posting_date", "-id"]
        permissions = [
            ("post_chequeevent", "Can record that the bank cleared or bounced a cheque"),
            (
                "cancel_chequeevent",
                "Can cancel a cheque clearing or bounce and reverse its entries",
            ),
            ("amend_chequeevent", "Can raise an amendment of a cancelled cheque event"),
        ]
        constraints = [
            # Partial unique index: many drafts, at most one posted. Without it,
            # a cheque could be both cleared and bounced and every balance would
            # be out by the cheque.
            models.UniqueConstraint(
                fields=["payment"],
                condition=models.Q(status=DocumentStatus.POSTED),
                name="chequeevent_one_settlement_per_payment",
                violation_error_message="That cheque has already been settled.",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.code} — {self.get_kind_display()} ({self.posting_date})"

    def get_absolute_url(self) -> str:
        """The payment's screen. A cheque event has no page of its own — it is
        shown, cancelled and amended from the payment it belongs to."""
        return reverse("payments:detail", kwargs={"pk": self.payment_id})

    @property
    def is_bounce(self) -> bool:
        return self.kind == ChequeEventKind.BOUNCED

    def dependents(self) -> list[Dependent]:
        """Nothing depends on a cheque event.

        It is the end of its own chain: a clearing or a bounce is the last thing
        that happens to a cheque, and cancelling one simply puts the cheque back
        to pending. Spelled out rather than inherited so the audit in
        ``tests/test_lifecycle.py`` reads the same for every document type.
        """
        return []

    # ------------------------------------------------------------------
    # Lifecycle. The work is in services; these are the entry points.
    # ------------------------------------------------------------------
    def post(self, *, user=None):
        from .services import post_cheque_event

        return post_cheque_event(self, user=user)

    def cancel(self, *, user=None, reason: str = ""):
        from .services import cancel_cheque_event

        return cancel_cheque_event(self, user=user, reason=reason)

    def amend(self, *, user=None):
        from .services import amend_cheque_event

        return amend_cheque_event(self, user=user)
