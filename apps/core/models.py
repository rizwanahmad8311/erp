"""
Base models. Everything except :class:`DocumentSequence` is abstract — there are
no domain models in this project yet, by design.

The bases exist so the locked rules in CLAUDE.md are enforced by inheritance
rather than by everyone remembering them.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone

from .enums import ALLOWED_STATUS_TRANSITIONS, DocumentStatus
from .exceptions import (
    AppendOnlyViolation,
    DocumentImmutable,
    IllegalTransition,
)
from .lifecycle import Dependent, TimelineStep, document_timeline
from .reporting import DocumentQuerySet


def _actor_fk(related_name: str = "+", **kwargs):
    """FK to the user who did something.

    ``PROTECT`` on purpose: a user who has touched financial records cannot be
    deleted out from under the audit trail. Deactivate them instead.
    """
    return models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name=related_name,
        **kwargs,
    )


class TimeStampedModel(models.Model):
    """Who touched this row, and when."""

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = _actor_fk("%(app_label)s_%(class)s_created")
    updated_by = _actor_fk("%(app_label)s_%(class)s_updated")

    class Meta:
        abstract = True


class AppendOnlyModel(models.Model):
    """Base for ledger and stock rows: insert only, forever.

    A row that has been written is history. Corrections are new reversing rows,
    never an UPDATE and never a DELETE — CLAUDE.md §3. Both operations raise
    here so the rule fails loudly in a test instead of quietly in a year-end
    report.

    There is no ``updated_by``: nothing here is ever updated.
    """

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    created_by = _actor_fk()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise AppendOnlyViolation(
                f"{type(self).__name__} is append-only; write a reversing row "
                f"instead of updating pk={self.pk}."
            )
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise AppendOnlyViolation(
            f"{type(self).__name__} is append-only; write a reversing row "
            f"instead of deleting pk={self.pk}."
        )


class DocumentModel(TimeStampedModel):
    """Base for every posting document: invoices, bills, receipts, adjustments.

    Lifecycle is DRAFT -> POSTED -> CANCELLED and nothing else (CLAUDE.md §5).
    A POSTED document is immutable; :meth:`save` enforces that rather than
    trusting callers.

    Concrete subclasses add their own party and line relations, and implement
    :meth:`post`, :meth:`cancel` and :meth:`amend`. They do not add cached
    totals that reports read — reports read the ledger (CLAUDE.md §6).

    The lifecycle contract
    ----------------------
    Every subclass implements exactly these three signatures, and every one of
    them delegates to a service function wrapped in ``transaction.atomic()``.
    Two documents whose ``cancel()`` differ in shape are two documents whose
    cancel screens will differ in behaviour, and only one of them will have been
    tested::

        def post(self, *, user=None, **options) -> Self
        def cancel(self, *, user=None, reason: str = "") -> Self
        def amend(self, *, user=None) -> Self

    ``**options`` is where a document type puts the one extra decision its
    posting needs — ``SalesInvoice.post(override_credit_limit=...)`` is the only
    one today. It is keyword-only and it always has a default, so every caller
    that knows nothing about a particular document type can still post it.

    Each service does the same five things in the same order:

    1. ``assert_transition()`` — refuse the move before anything is written;
    2. the document's own preconditions (lines exist, credit limit, shape);
    3. for a cancellation, :meth:`assert_cancellable` — refuse while something
       still depends on this document;
    4. the ledger and stock writes, or their reversals;
    5. ``mark_posted()`` / ``mark_cancelled()`` and one ``save()``.

    Amending never writes to a ledger at all: it clones a CANCELLED document
    into a fresh DRAFT through :meth:`build_amendment` and returns it.
    """

    #: Fields a cancellation is allowed to write on an otherwise-frozen document.
    CANCELLATION_FIELDS = frozenset(
        {
            "status",
            "cancelled_at",
            "cancelled_by_id",
            "cancel_reason",
            "updated_at",
            "updated_by_id",
        }
    )

    code = models.CharField(
        max_length=32,
        unique=True,
        editable=False,
        help_text="Allocated by apps.core.services.get_next_code, e.g. SI-2026-000123.",
    )
    status = models.CharField(
        max_length=16,
        choices=DocumentStatus.choices,
        default=DocumentStatus.DRAFT,
        db_index=True,
    )

    posted_at = models.DateTimeField(null=True, blank=True)
    posted_by = _actor_fk("%(app_label)s_%(class)s_posted")

    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = _actor_fk("%(app_label)s_%(class)s_cancelled")
    cancel_reason = models.TextField(blank=True, default="")

    amended_from = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="%(app_label)s_%(class)s_amendments",
        help_text="The cancelled document this one replaces.",
    )
    amendment_no = models.PositiveIntegerField(
        default=0,
        help_text="0 for an original document, 1 for its first amendment, and so on.",
    )

    #: Inherited by every document, so ``live()``, ``cancelled()`` and
    #: ``for_report()`` mean the same thing on all of them — see
    #: :mod:`apps.core.reporting`.
    objects = DocumentQuerySet.as_manager()

    class Meta:
        abstract = True
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.code} ({self.get_status_display()})"

    # ------------------------------------------------------------------
    # Immutability
    # ------------------------------------------------------------------
    def save(self, *args, **kwargs):
        self._assert_save_allowed()
        super().save(*args, **kwargs)
        # Re-snapshot so a second save() in the same request compares against
        # what is now in the database.
        self._loaded_state = {
            field.attname: getattr(self, field.attname) for field in self._meta.concrete_fields
        }

    @classmethod
    def from_db(cls, db, field_names, values):
        """Snapshot what was loaded so :meth:`save` can see what changed."""
        instance = super().from_db(db, field_names, values)
        instance._loaded_state = dict(zip(field_names, values, strict=True))
        return instance

    def _changed_fields(self) -> set[str]:
        """Concrete local fields whose value differs from what was loaded.

        Deferred fields are skipped: they were never loaded, so they cannot have
        been changed on this instance.
        """
        loaded = getattr(self, "_loaded_state", None)
        if loaded is None:
            return set()
        return {
            field.attname
            for field in self._meta.concrete_fields
            if field.attname in loaded and getattr(self, field.attname) != loaded[field.attname]
        }

    def _assert_save_allowed(self) -> None:
        loaded = getattr(self, "_loaded_state", None)
        if self.pk is None or loaded is None:
            return  # a fresh insert, or an instance we did not load from the DB

        was = loaded.get("status")
        if was == DocumentStatus.DRAFT:
            return  # drafts are freely editable

        changed = self._changed_fields()
        if not changed:
            return

        if was == DocumentStatus.POSTED:
            illegal = changed - self.CANCELLATION_FIELDS
            if not illegal and self.status == DocumentStatus.CANCELLED:
                return  # this is the cancellation itself
            offending = sorted(illegal or changed)
            raise DocumentImmutable(
                f"{type(self).__name__} {self.code} is POSTED and cannot be modified "
                f"(attempted to change: {', '.join(offending)}). "
                f"Cancel it and post an amendment instead."
            )

        if was == DocumentStatus.CANCELLED:
            raise DocumentImmutable(
                f"{type(self).__name__} {self.code} is CANCELLED and cannot be modified "
                f"(attempted to change: {', '.join(sorted(changed))})."
            )

    def delete(self, *args, **kwargs):
        """Drafts may be deleted; anything that has touched the ledger may not.

        CLAUDE.md §5 says a document is never deleted. A DRAFT has written
        nothing to any ledger and has no reversing entries to lose, so deleting
        one destroys no financial history — that is the only case allowed here.
        """
        if self.status != DocumentStatus.DRAFT:
            raise DocumentImmutable(
                f"{type(self).__name__} {self.code} is {self.status} and cannot be "
                f"deleted. Cancel it instead, which posts reversing entries."
            )
        return super().delete(*args, **kwargs)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    @property
    def is_editable(self) -> bool:
        return self.status == DocumentStatus.DRAFT

    def assert_transition(self, new_status: str) -> None:
        """Guard the lifecycle. Call at the top of a posting service."""
        allowed = ALLOWED_STATUS_TRANSITIONS.get(self.status, set())
        if new_status not in allowed:
            raise IllegalTransition(
                f"{type(self).__name__} {self.code}: cannot move {self.status} -> {new_status}."
            )

    def assert_editable(self) -> None:
        if not self.is_editable:
            raise DocumentImmutable(
                f"{type(self).__name__} {self.code} is {self.status} and cannot be "
                f"modified. Cancel it and post an amendment."
            )

    def post(self, *, user=None, **options):
        """Validate, write ledger and stock entries, set POSTED.

        Subclasses implement this. The implementation must be wrapped in
        ``transaction.atomic()`` and must assert that debits equal credits
        before it returns (CLAUDE.md §4).

        ``**options`` carries a document type's own posting decision and always
        has a default — see the class docstring.
        """
        raise NotImplementedError(f"{type(self).__name__} must implement post()")

    def cancel(self, *, user=None, reason: str = ""):
        """Write REVERSING ledger and stock entries, set CANCELLED.

        Subclasses implement this. Reversal means writing new rows with the
        opposite sign — never updating or deleting the originals (CLAUDE.md §3).
        Every implementation calls :meth:`assert_cancellable` before it writes
        anything.
        """
        raise NotImplementedError(f"{type(self).__name__} must implement cancel()")

    def amend(self, *, user=None):
        """Clone this CANCELLED document into a new DRAFT amendment.

        Subclasses implement this: call :meth:`build_amendment` for the header,
        then copy their own lines onto the result.
        """
        raise NotImplementedError(f"{type(self).__name__} must implement amend()")

    # ------------------------------------------------------------------
    # What blocks a cancellation
    # ------------------------------------------------------------------
    def dependents(self) -> list[Dependent]:
        """Everything that would be left dangling if this were reversed.

        Empty by default: a document with nothing hanging off it cancels freely.
        Subclasses return :class:`~apps.core.lifecycle.Dependent` records for
        the things that do — money allocated to an invoice, a credit note raised
        against it, a cheque event recording what the bank did — **most specific
        first**, because the first one names the refusal.

        This is a **read**, and the cancel screen calls it directly to show what
        blocks before anybody presses anything. It must not raise.
        """
        return []

    def assert_cancellable(self) -> None:
        """Raise unless nothing depends on this document.

        Called at the top of every ``cancel_*`` service, before a single row is
        written. The message names every blocker and what to do about each one;
        the exception class comes from the first blocker, so "this invoice has
        been paid" and "there is a credit note against it" are told apart by
        anything that wants to catch one and not the other.
        """
        blockers = self.dependents()
        if not blockers:
            return

        listed = "; ".join(str(blocker) for blocker in blockers)
        actions = " ".join(dict.fromkeys(blocker.action for blocker in blockers if blocker.action))
        raise blockers[0].error(
            f"{type(self).__name__} {self.code} cannot be cancelled: {listed}. {actions}".strip(),
            dependents=blockers,
        )

    @property
    def may_cancel(self) -> bool:
        """Whether cancelling would be accepted right now. For the screen only."""
        return self.status == DocumentStatus.POSTED and not self.dependents()

    @classmethod
    def post_permission(cls) -> str:
        """``"sales.post_salesinvoice"`` — the permission posting wants.

        Separate from ``change_`` on purpose. Writing a bill and committing it
        to the ledger are different acts with different consequences: a DRAFT
        has touched nothing, and a POSTED document is immutable and has money
        behind it (CLAUDE.md §5). An Operator does the first all day; who may do
        the second is a decision the office makes per person.
        """
        return f"{cls._meta.app_label}.post_{cls._meta.model_name}"

    @classmethod
    def cancel_permission(cls) -> str:
        """``"sales.cancel_salesinvoice"`` — the permission the cancel view wants.

        Derived rather than spelled out per view, so a new document type cannot
        get a cancel screen with no permission behind it by forgetting a string.
        The permission itself is declared in each concrete model's
        ``Meta.permissions``; :func:`tests.test_lifecycle` fails the build if one
        is missing.
        """
        return f"{cls._meta.app_label}.cancel_{cls._meta.model_name}"

    @classmethod
    def amend_permission(cls) -> str:
        """``"sales.amend_salesinvoice"`` — the permission amending wants.

        Held with ``cancel_`` in practice, because a document has to be
        cancelled before it can be amended (CLAUDE.md §5) — but declared
        separately, because the two are separate questions and a role that may
        reverse a mistake is not automatically a role that may replace it.
        """
        return f"{cls._meta.app_label}.amend_{cls._meta.model_name}"

    # ``None`` counts as allowed on all three of these: a caller with no user is
    # a management command, a data migration or a test harness, all of which are
    # already trusted code. A request always has a user, so nothing reachable
    # from a browser gets in this way.
    def user_may_post(self, user) -> bool:
        """Whether this user holds the post permission for this document type."""
        if user is None:
            return True
        return user.has_perm(self.post_permission())

    def user_may_cancel(self, user) -> bool:
        """Whether this user holds the cancel permission for this document type."""
        if user is None:
            return True
        return user.has_perm(self.cancel_permission())

    def user_may_amend(self, user) -> bool:
        """Whether this user holds the amend permission for this document type."""
        if user is None:
            return True
        return user.has_perm(self.amend_permission())

    # ------------------------------------------------------------------
    # The timeline
    # ------------------------------------------------------------------
    def timeline(self) -> list[TimelineStep]:
        """Created -> posted -> cancelled -> amended into, oldest first.

        Rendered by ``templates/core/partials/timeline.html`` on every document
        detail page. Read straight off this row and the amendment chain; nothing
        is stored and nothing is inferred.
        """
        return document_timeline(self)

    # ------------------------------------------------------------------
    # Amendment helpers (concrete — subclass amend() builds on these)
    # ------------------------------------------------------------------
    def root_document(self) -> DocumentModel:
        """Walk back to the original document at the head of the amend chain."""
        document = self
        seen = {document.pk}
        while document.amended_from_id:
            document = document.amended_from
            if document.pk in seen:  # defensive: a cycle would hang the request
                raise IllegalTransition(f"Amendment chain for {self.code} contains a cycle.")
            seen.add(document.pk)
        return document

    def amendments(self):
        """The documents that amend this one, oldest first.

        A queryset rather than the generated reverse accessor
        (``sales_salesinvoice_amendments``), whose name changes with the app and
        the model and so cannot be written in a shared template or a base-class
        method. In practice there is at most one — a document is amended once,
        and its amendment is amended after that — but this does not assume it,
        because assuming it would silently drop the second row if it ever
        appeared.
        """
        return type(self).objects.filter(amended_from_id=self.pk).order_by("pk")

    def next_amendment(self):
        """The draft that replaced this document, or ``None`` if none has."""
        return self.amendments().first()

    def chain(self) -> list[DocumentModel]:
        """The whole amendment lineage, oldest first, this document included.

        ``[SI-2026-000123, SI-2026-000123-1, SI-2026-000123-2]`` — the original
        and every correction after it, whichever link you happen to be holding.
        This is what the timeline walks in both directions and what the cancel
        screen shows so nobody reverses the wrong generation of the same bill.

        Walks back to the root and then forward, one query per link. Chains are
        two or three documents long in practice; a chain long enough for that to
        matter is a document somebody should stop amending.
        """
        chain = [self.root_document()]
        seen = {chain[0].pk}
        while True:
            following = chain[-1].next_amendment()
            if following is None:
                break
            if following.pk in seen:  # defensive: a cycle would loop forever
                raise IllegalTransition(f"Amendment chain for {self.code} contains a cycle.")
            seen.add(following.pk)
            chain.append(following)
        return chain

    def next_amendment_code(self) -> str:
        """``SI-2026-000123`` -> ``SI-2026-000123-1`` -> ``SI-2026-000123-2``.

        The suffix is derived from the *root* document's code rather than by
        stripping digits off this one — ``SI-2026-000123`` already ends in a
        run of digits, so any regex that peeled off a trailing ``-NNN`` would
        eat the serial number itself.
        """
        return f"{self.root_document().code}-{self.amendment_no + 1}"

    #: Fields never carried onto an amendment: identity and lifecycle state.
    AMENDMENT_EXCLUDED_FIELDS = frozenset(
        {
            "id",
            "code",
            "status",
            "posted_at",
            "posted_by_id",
            "cancelled_at",
            "cancelled_by_id",
            "cancel_reason",
            "amended_from_id",
            "amendment_no",
            "created_at",
            "updated_at",
            "created_by_id",
            "updated_by_id",
        }
    )

    def build_amendment(self, *, user=None, **overrides):
        """Create and save the header of a new DRAFT amending this document.

        Requires this document to be CANCELLED: an amendment replaces something
        that has already been reversed out of the ledger, so amending a POSTED
        document would double-count it.

        Returns the saved new instance. The caller copies lines onto it and
        posts it when ready.
        """
        if self.status != DocumentStatus.CANCELLED:
            raise IllegalTransition(
                f"{type(self).__name__} {self.code} is {self.status}; only a CANCELLED "
                f"document can be amended. Cancel it first."
            )

        # A document is amended **once**. Amending it a second time forks the
        # chain: both replacements would be numbered from the same root and
        # would claim the same code, which used to surface as a raw
        # IntegrityError on the second one. Worse than the traceback is what it
        # means — two live documents each claiming to be the correction of the
        # same reversed bill, which is the correction counted twice.
        #
        # The chain is linear on purpose (CLAUDE.md §5): SI-2026-000123 -> -1 ->
        # -2, never two -1s. To correct an amendment, cancel *it* and amend that.
        #
        # Found by tests/test_invariants_property.py, which generated
        # sell -> cancel -> amend -> amend against the same document.
        existing = self.amendments().first()
        if existing is not None:
            raise IllegalTransition(
                f"{type(self).__name__} {self.code} has already been amended into "
                f"{existing.code}. A document is amended once — to correct the "
                f"amendment, cancel {existing.code} and amend that instead."
            )

        carried = {
            field.attname: getattr(self, field.attname)
            for field in self._meta.concrete_fields
            if field.attname not in self.AMENDMENT_EXCLUDED_FIELDS
        }
        carried.update(overrides)

        return type(self).objects.create(
            code=self.next_amendment_code(),
            status=DocumentStatus.DRAFT,
            amended_from=self,
            amendment_no=self.amendment_no + 1,
            created_by=user,
            updated_by=user,
            **carried,
        )

    def mark_posted(self, *, user=None, when=None) -> None:
        """Set the POSTED stamps in memory. The caller saves inside its atomic block."""
        self.assert_transition(DocumentStatus.POSTED)
        self.status = DocumentStatus.POSTED
        self.posted_at = when or timezone.now()
        self.posted_by = user
        self.updated_by = user

    def mark_cancelled(self, *, user=None, reason: str = "", when=None) -> None:
        """Set the CANCELLED stamps in memory. The caller saves inside its atomic block."""
        self.assert_transition(DocumentStatus.CANCELLED)
        self.status = DocumentStatus.CANCELLED
        self.cancelled_at = when or timezone.now()
        self.cancelled_by = user
        self.cancel_reason = reason
        self.updated_by = user


class DocumentSequence(models.Model):
    """Per-prefix, per-fiscal-year counter behind ``get_next_code``.

    One row per (prefix, fiscal_year). This is infrastructure, not a domain
    model: it holds no business data, only the last number handed out.

    Never edit ``last_number`` by hand. Lowering it hands out a code that
    already exists and the unique constraint on the document will reject it at
    the worst possible moment.
    """

    prefix = models.CharField(max_length=8)
    fiscal_year = models.PositiveIntegerField()
    last_number = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "document sequence"
        ordering = ["prefix", "fiscal_year"]
        constraints = [
            models.UniqueConstraint(
                fields=["prefix", "fiscal_year"],
                name="core_documentsequence_unique_prefix_year",
            )
        ]

    def __str__(self) -> str:
        return f"{self.prefix}-{self.fiscal_year} @ {self.last_number}"
