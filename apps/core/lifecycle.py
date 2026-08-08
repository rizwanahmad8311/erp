"""The three things every document does, described once.

:class:`~apps.core.models.DocumentModel` says a document is DRAFT, then POSTED,
then possibly CANCELLED, and that a cancelled one may be amended into a new
draft. This module holds the vocabulary that goes with those moves and that no
single app owns:

:class:`Dependent`
    One thing standing in the way of a cancellation. Every document type answers
    the same question — ``dependents()`` — and the cancel screen lists what comes
    back before anybody presses the button.

:class:`TimelineStep`
    One event in a document's life: created, posted, cancelled, amended into,
    amended from. The timeline strip on every detail page is a loop over these.

:func:`payment_allocations`
    The seam between "a document" and "money applied to it". It lives here, not
    in purchasing, because sales asks the identical question and an app importing
    another app's services to answer it is how two apps end up with two answers.

Nothing here writes anything.
"""

from __future__ import annotations

from typing import NamedTuple

from .enums import DocumentStatus
from .exceptions import DocumentHasDependents, PaymentAllocated
from .money import fmt


class Dependent(NamedTuple):
    """Something that would be left dangling if a document were reversed.

    ``kind`` is the word an operator uses for it ("payment", "credit note"),
    ``code`` identifies it, ``detail`` says why it blocks, and ``action`` says
    what to do about it. The four are kept apart rather than pre-formatted into
    a sentence so the cancel screen can lay them out as a table and link
    ``code`` to the document itself.

    ``error`` is the exception :meth:`~apps.core.models.DocumentModel.assert_cancellable`
    raises when this blocker is the first one standing in the way. It rides on
    the blocker rather than on the document type because a sales invoice can be
    blocked by two different things — money allocated to it, or a credit note
    raised against it — and the refusal should be named after whichever one it
    actually is. Every value is a :class:`~apps.core.exceptions.DocumentHasDependents`
    subclass, so one ``except`` still catches all of them.
    """

    kind: str
    code: str
    detail: str
    action: str
    #: The blocking document, when the caller has it in hand. Used for links.
    document: object | None = None
    error: type = DocumentHasDependents

    def __str__(self) -> str:
        return f"{self.kind} {self.code} — {self.detail}"


class TimelineStep(NamedTuple):
    """One event in a document's life, for the timeline strip.

    ``at`` and ``by`` are whatever was stamped at the time and may be ``None``:
    a document created by a management command has no user, and a draft has no
    posting date yet. The template shows what is there and says nothing about
    what is not.
    """

    kind: str
    label: str
    at: object | None = None
    by: object | None = None
    #: Another document this step points at — the one amended, or the amendment.
    document: object | None = None
    note: str = ""

    @property
    def is_done(self) -> bool:
        """Whether this step has actually happened, or is only the next one."""
        return self.at is not None or self.document is not None


# ===========================================================================
# The payments seam
# ===========================================================================
class Allocation(NamedTuple):
    """One payment applied to one document.

    The whole of what a sales or purchase document knows about a payment: what
    it is called, and how much of it landed here.
    """

    code: str
    amount_paisa: int


def payment_allocations(document) -> list[Allocation]:
    """Every live payment allocated against this document.

    The seam that keeps ``paid_paisa`` a property rather than a column
    (CLAUDE.md §6), and the reason a document can refuse to be cancelled without
    core importing :mod:`apps.payments`. The contract it asks for is one
    function::

        # apps/payments/services.py
        def allocations_for(document) -> Iterable[Allocation]: ...

    Asking and finding nothing is deliberate: an installation without the
    payments app gets an empty list rather than an ImportError, and the day the
    function appears every caller starts getting real figures with no change
    here.
    """
    try:
        from apps.payments import services as payments_services
    except ImportError:  # pragma: no cover - payments is installed in this build
        return []

    resolver = getattr(payments_services, "allocations_for", None)
    if resolver is None:  # pragma: no cover - the function exists in this build
        return []
    return [Allocation(str(item.code), int(item.amount_paisa)) for item in resolver(document)]


def payment_dependents(document) -> list[Dependent]:
    """The allocated payments, as cancellation blockers.

    A payment is its own voucher with its own ledger rows. Reversing this
    document would leave that payment sitting against a party balance with no
    bill under it — money paid against nothing.
    """
    return [
        Dependent(
            kind="payment",
            code=allocation.code,
            detail=f"{fmt(allocation.amount_paisa)} allocated to this document",
            action="Unallocate it, then cancel.",
            error=PaymentAllocated,
        )
        for allocation in payment_allocations(document)
    ]


# ===========================================================================
# The timeline
# ===========================================================================
def document_timeline(document) -> list[TimelineStep]:
    """A document's life, oldest first, with who did what and when.

    Five kinds of step, and every one of them is read off the document itself or
    off the amendment chain — nothing here is stored and nothing is inferred
    from the ledger:

    ``amended_from``  the cancelled document this one replaces, if any
    ``created``       always present
    ``posted``        present once it has been posted
    ``cancelled``     present once it has been cancelled, with the reason
    ``amended_to``    the draft that replaced it, if one has been made

    A CANCELLED document with no amendment yet still gets an ``amended_to``
    step, marked not-done, because "this was reversed and nothing replaced it"
    is exactly the state somebody looking at the screen needs to notice.
    """
    steps: list[TimelineStep] = []

    previous = document.amended_from
    if previous is not None:
        steps.append(
            TimelineStep(
                kind="amended_from",
                label="Amends",
                at=previous.cancelled_at,
                by=previous.cancelled_by,
                document=previous,
                note=previous.cancel_reason,
            )
        )

    steps.append(
        TimelineStep(
            kind="created",
            label="Created",
            at=document.created_at,
            by=document.created_by,
        )
    )
    steps.append(
        TimelineStep(
            kind="posted",
            label="Posted",
            at=document.posted_at,
            by=document.posted_by,
        )
    )

    if document.status == DocumentStatus.CANCELLED or document.cancelled_at is not None:
        steps.append(
            TimelineStep(
                kind="cancelled",
                label="Cancelled",
                at=document.cancelled_at,
                by=document.cancelled_by,
                note=document.cancel_reason,
            )
        )

        amendment = document.next_amendment()
        steps.append(
            TimelineStep(
                kind="amended_to",
                label="Amended into" if amendment is not None else "Not yet amended",
                at=amendment.created_at if amendment is not None else None,
                by=amendment.created_by if amendment is not None else None,
                document=amendment,
                note=(
                    ""
                    if amendment is not None
                    else "Nothing has replaced this document. Amend it to post a corrected copy."
                ),
            )
        )

    return steps


__all__ = [
    "Allocation",
    "Dependent",
    "TimelineStep",
    "document_timeline",
    "payment_allocations",
    "payment_dependents",
]
