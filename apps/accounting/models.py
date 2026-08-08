"""
Chart of accounts and the append-only general ledger.

:class:`LedgerEntry` is the source of truth for every figure this system will
ever print. Nothing else is. There is no cached balance field on an account, on
a party, or on a document header, and there must never be one (CLAUDE.md §6) —
a cached total is a number that can disagree with the ledger, and once one has
disagreed for a week nobody can tell you which of the two is right.

The rows are therefore append-only (CLAUDE.md §3): written once, never updated,
never deleted. A correction is a new row with the sides swapped, pointing at the
row it reverses.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import models

from apps.core.exceptions import AppendOnlyViolation
from apps.core.fields import MoneyField
from apps.core.models import AppendOnlyModel, TimeStampedModel

from .enums import AccountType, PartyType, account_sign
from .exceptions import GroupAccountPosting, InactiveAccount, InvalidAccount, InvalidPosting


class Account(TimeStampedModel):
    """A node in the chart of accounts.

    The chart is a tree. Interior nodes (``is_group=True``) exist to be totalled
    and hold no entries of their own; leaves receive the entries. Both facts are
    enforced rather than documented — see :meth:`assert_postable` and
    :meth:`_assert_structure`.

    Unlike a document this is a master record: it is editable, and it is *not*
    append-only. What is guarded is the handful of edits that would re-sign or
    re-shape history, such as changing ``type`` or turning an account that
    already has entries into a group.
    """

    code = models.CharField(
        max_length=16,
        unique=True,
        help_text="Stable identifier, e.g. 1110. Sorting by code gives chart order.",
    )
    name = models.CharField(max_length=128)
    type = models.CharField(
        max_length=16,
        choices=AccountType.choices,
        db_index=True,
        help_text="Decides whether a debit raises or lowers this account. Not a label.",
    )
    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="children",
        help_text="The group this account sits under. Empty for a root.",
    )
    is_group = models.BooleanField(
        default=False,
        help_text="A group is a heading: it is totalled from its children and never posted to.",
    )
    is_active = models.BooleanField(
        default=True,
        help_text="Deactivating blocks new postings. It never hides or alters history.",
    )

    class Meta:
        ordering = ["code"]
        verbose_name = "account"
        verbose_name_plural = "chart of accounts"

    def __str__(self) -> str:
        return f"{self.code} — {self.name}"

    # ------------------------------------------------------------------
    # Structure
    # ------------------------------------------------------------------
    @property
    def is_postable(self) -> bool:
        return self.is_active and not self.is_group

    def assert_postable(self) -> None:
        """Raise unless this account may receive a *new* entry.

        Called by :func:`apps.accounting.services.post_entries` for every line.
        Reversals do not call it: an account can be deactivated between posting
        an invoice and cancelling it, and that must not trap the document in a
        state it cannot leave.
        """
        if self.is_group:
            raise GroupAccountPosting(
                f"Account {self.code} ({self.name}) is a group and cannot receive entries. "
                f"Post to one of its children instead."
            )
        if not self.is_active:
            raise InactiveAccount(
                f"Account {self.code} ({self.name}) is inactive and cannot receive new entries."
            )

    def ancestors(self) -> list[Account]:
        """Root-last walk up the tree. Cheap: charts are shallow."""
        chain: list[Account] = []
        seen = {self.pk}
        node = self
        while node.parent_id:
            node = node.parent
            if node.pk in seen:  # defensive; _assert_structure prevents cycles
                raise InvalidAccount(f"Account {self.code} sits in a parent cycle.")
            seen.add(node.pk)
            chain.append(node)
        return chain

    def subtree_ids(self) -> list[int]:
        """This account's id plus every descendant's.

        A leaf is its own subtree, so callers never need to branch on
        ``is_group``. This is what makes ``account_balance(expenses_group)``
        return the total of the group rather than a confident zero.

        Walked one level at a time rather than with a recursive CTE: the chart
        is a few dozen rows two or three levels deep, and this stays portable.
        """
        if not self.is_group:
            return [self.pk]

        ids = [self.pk]
        frontier = [self.pk]
        seen = {self.pk}
        while frontier:
            children = list(
                Account.objects.filter(parent_id__in=frontier)
                .exclude(pk__in=seen)
                .values_list("pk", flat=True)
            )
            if not children:
                break
            ids.extend(children)
            seen.update(children)
            frontier = children
        return ids

    # ------------------------------------------------------------------
    # Integrity
    # ------------------------------------------------------------------
    def _assert_structure(self) -> None:
        """The invariants that keep the tree addable.

        Runs on every save. Accounts are written a few dozen times in the life
        of an installation, so the queries here cost nothing, and every one of
        these mistakes is silent and expensive if it lands.
        """
        if self.parent_id is not None:
            if self.parent_id == self.pk:
                raise InvalidAccount(f"Account {self.code} cannot be its own parent.")

            parent = self.parent
            if not parent.is_group:
                raise InvalidAccount(
                    f"Account {self.code} cannot sit under {parent.code} ({parent.name}): "
                    f"a parent must be a group."
                )
            if parent.type != self.type:
                raise InvalidAccount(
                    f"Account {self.code} is {self.type} but its parent {parent.code} is "
                    f"{parent.type}. A subtree has to total to a single type."
                )
            if self.pk is not None and self.pk in {a.pk for a in parent.ancestors()}:
                raise InvalidAccount(
                    f"Making {parent.code} the parent of {self.code} would create a cycle."
                )

        if self.pk is None:
            return

        # Flipping is_group either strands existing entries under a heading, or
        # strands existing children under a leaf.
        was_group = Account.objects.filter(pk=self.pk).values_list("is_group", flat=True).first()
        if was_group is None or was_group == self.is_group:
            return

        if self.is_group and LedgerEntry.objects.filter(account_id=self.pk).exists():
            raise InvalidAccount(
                f"Account {self.code} already has ledger entries and cannot become a group. "
                f"Create a new group and move future postings to a child of it."
            )
        if not self.is_group and Account.objects.filter(parent_id=self.pk).exists():
            raise InvalidAccount(f"Account {self.code} has children and cannot stop being a group.")

    def clean(self):
        """Surface :meth:`_assert_structure` as a form error in the admin."""
        super().clean()
        try:
            self._assert_structure()
        except InvalidAccount as exc:
            raise ValidationError(str(exc)) from exc

    def save(self, *args, **kwargs):
        self._assert_structure()
        return super().save(*args, **kwargs)

    @property
    def natural_sign(self) -> int:
        """``+1`` for a debit-normal account, ``-1`` for a credit-normal one."""
        return account_sign(self.type)


class LedgerEntryQuerySet(models.QuerySet):
    """Closes the doors ``AppendOnlyModel`` cannot reach.

    ``AppendOnlyModel`` guards ``instance.save()`` and ``instance.delete()``,
    which is every route a person takes deliberately. The routes people take
    *by accident* are the bulk ones — a ``filter(...).update(...)`` in a data
    migration or a shell session, which never loads an instance and so never
    hits that guard. CLAUDE.md §3 forbids them; this makes them raise.
    """

    def update(self, **kwargs):
        raise AppendOnlyViolation(
            "LedgerEntry is append-only; QuerySet.update() would rewrite posted history. "
            "Post a reversing entry with accounting.services.reverse_entries() instead."
        )

    def delete(self):
        raise AppendOnlyViolation(
            "LedgerEntry is append-only; QuerySet.delete() would erase posted history. "
            "Post a reversing entry with accounting.services.reverse_entries() instead."
        )


class LedgerEntryManager(models.Manager.from_queryset(LedgerEntryQuerySet)):
    """Manager for :class:`LedgerEntry`. Inserts only."""

    def bulk_update(self, *args, **kwargs):
        raise AppendOnlyViolation("LedgerEntry is append-only; bulk_update() is not available.")

    def bulk_create(self, objs, *args, **kwargs):
        # This is the one bulk write that is allowed — post_entries uses it —
        # but update_conflicts turns an INSERT into an UPSERT, which is an
        # UPDATE wearing a hat.
        if kwargs.get("update_conflicts"):
            raise AppendOnlyViolation(
                "LedgerEntry is append-only; bulk_create(update_conflicts=True) would "
                "rewrite existing rows."
            )
        return super().bulk_create(objs, *args, **kwargs)


class LedgerEntry(AppendOnlyModel):
    """One side of one posting. Written once, never changed.

    Every row records a single debit **or** a single credit — never both, never
    a negative. That is why a reversal is a row on the *opposite* side rather
    than a negative amount: a ledger where "credit 500" and "debit -500" both
    exist has two ways to say the same thing, and every report then has to know
    about both. One representation, enforced by a database CHECK.

    The link back to the document that caused the row is soft: a type name, an
    id and a denormalised code. No foreign key, because the ledger spans a
    dozen document models and must outlive any of them.
    """

    objects = LedgerEntryManager()

    posting_date = models.DateField(
        db_index=True,
        help_text="The day this hits the books. Not the day the row was written.",
    )
    account = models.ForeignKey(
        Account,
        on_delete=models.PROTECT,
        related_name="entries",
        help_text="PROTECT: an account with history cannot be deleted out from under it.",
    )

    debit_paisa = MoneyField(non_negative=True)
    credit_paisa = MoneyField(non_negative=True)

    # Nullable on purpose, against DJ001's usual "no NULL on a CharField"
    # advice. That rule exists to stop a field having two empty values, "" and
    # NULL — here the party_is_a_pair CHECK forbids "" outright, and pairing a
    # NULL string with the NULL integer beside it is what lets one constraint
    # and one index cover both halves of the link.
    party_type = models.CharField(  # noqa: DJ001
        max_length=8,
        choices=PartyType.choices,
        null=True,
        blank=True,
        help_text="NULL on rows that are not a receivable or payable.",
    )
    party_id = models.BigIntegerField(
        null=True,
        blank=True,
        help_text="Soft link to masters. Deliberately not a foreign key.",
    )

    voucher_type = models.CharField(
        max_length=64,
        help_text='The document model name, e.g. "SalesInvoice".',
    )
    voucher_id = models.BigIntegerField()
    voucher_code = models.CharField(
        max_length=32,
        help_text="Denormalised document code, so reports never join to find it.",
    )

    is_reversal = models.BooleanField(default=False)
    reverses = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="reversed_by",
        help_text="The row this one cancels out. Set on reversals, NULL otherwise.",
    )

    remarks = models.TextField(blank=True, default="")

    class Meta:
        verbose_name = "ledger entry"
        verbose_name_plural = "ledger entries"
        ordering = ["posting_date", "id"]
        indexes = [
            # Account statements and every balance: "this account, up to this date".
            models.Index(fields=["account", "posting_date"], name="ledger_account_date_idx"),
            # Party ledgers, ageing and recovery.
            models.Index(fields=["party_type", "party_id"], name="ledger_party_idx"),
            # Finding a voucher's rows, which is what reversal does.
            models.Index(fields=["voucher_type", "voucher_id"], name="ledger_voucher_idx"),
        ]
        constraints = [
            # Redundant against exactly_one_side below — a negative amount fails
            # that one too — and kept anyway. It states the rule on its own
            # terms, so loosening one constraint does not quietly permit
            # negative money in the ledger.
            models.CheckConstraint(
                name="ledgerentry_amounts_non_negative",
                condition=models.Q(debit_paisa__gte=0) & models.Q(credit_paisa__gte=0),
                violation_error_message="Ledger amounts are never negative; swap the side instead.",
            ),
            models.CheckConstraint(
                name="ledgerentry_exactly_one_side",
                condition=(
                    models.Q(debit_paisa__gt=0, credit_paisa=0)
                    | models.Q(debit_paisa=0, credit_paisa__gt=0)
                ),
                violation_error_message="A ledger entry is exactly one non-zero debit or credit.",
            ),
            # A party is a (type, id) pair or nothing at all. Half of one is a
            # row that a party ledger will either miss or double-count.
            models.CheckConstraint(
                name="ledgerentry_party_is_a_pair",
                condition=(
                    models.Q(party_type__isnull=True, party_id__isnull=True)
                    | models.Q(party_type__isnull=False, party_id__isnull=False)
                ),
                violation_error_message="party_type and party_id are set together or not at all.",
            ),
            models.CheckConstraint(
                name="ledgerentry_reversal_points_at_something",
                condition=(
                    models.Q(is_reversal=True, reverses__isnull=False)
                    | models.Q(is_reversal=False, reverses__isnull=True)
                ),
                violation_error_message="A reversal names the row it reverses; nothing else does.",
            ),
            # The database's own answer to double reversal. reverse_entries
            # refuses it in Python with a readable error; this makes it
            # impossible even if two cancellations race.
            models.UniqueConstraint(
                fields=["reverses"],
                condition=models.Q(reverses__isnull=False),
                name="ledgerentry_one_reversal_per_row",
                violation_error_message="That entry has already been reversed.",
            ),
        ]

    def __str__(self) -> str:
        side = f"Dr {self.debit_paisa}" if self.debit_paisa else f"Cr {self.credit_paisa}"
        return f"{self.posting_date} {self.account_id} {side} ({self.voucher_code})"

    # ------------------------------------------------------------------
    # Append-only
    # ------------------------------------------------------------------
    def save(self, *args, **kwargs):
        """Validate an insert, then hand over to the append-only guard.

        ``super().save()`` is :meth:`AppendOnlyModel.save`, which raises on any
        write to a row that already has a pk. That guard is not repeated here —
        one implementation of "never UPDATE" is what makes it trustworthy.

        Validation is scoped to the insert on purpose. A row with a pk is not
        going to be written whatever its contents are, and reporting "your
        amounts are wrong" to someone editing posted history would invite them
        to fix the amounts and try again. They need to hear the real answer,
        which is that the row is permanent.

        Note that :meth:`assert_valid` does **not** run on the ``bulk_create``
        path, which is what ``post_entries`` uses. That path is covered twice
        over: the service validates each line before building the objects, and
        the CHECK constraints above catch anything that gets past it.
        """
        if self.pk is None:
            self.assert_valid()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        """Always raises. A posted row is history.

        ``AppendOnlyModel`` already refuses this; it is spelled out again here
        so the error names the voucher rather than just the model, and so that
        removing the base class does not quietly remove the guard.
        """
        raise AppendOnlyViolation(
            f"LedgerEntry pk={self.pk} ({self.voucher_type} {self.voucher_code}) cannot be "
            f"deleted — the ledger is append-only. Reverse the voucher instead."
        )

    def assert_valid(self) -> None:
        """Everything the CHECK constraints enforce, raised in Python first.

        The constraints are the real guarantee; this exists so a mistake fails
        with a sentence explaining it rather than with an ``IntegrityError``
        naming a constraint.
        """
        for name in ("debit_paisa", "credit_paisa"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise InvalidPosting(
                    f"{name} must be whole paisa as an int, got {type(value).__name__}: {value!r}"
                )
            if value < 0:
                raise InvalidPosting(
                    f"{name} is {value}; ledger amounts are never negative. "
                    f"A reversal is written on the opposite side, not as a minus."
                )

        if bool(self.debit_paisa) == bool(self.credit_paisa):
            raise InvalidPosting(
                f"A ledger entry is exactly one non-zero debit or credit; got "
                f"debit_paisa={self.debit_paisa}, credit_paisa={self.credit_paisa}."
            )

        if (self.party_type is None) != (self.party_id is None):
            raise InvalidPosting(
                f"party_type and party_id are set together or not at all; got "
                f"party_type={self.party_type!r}, party_id={self.party_id!r}."
            )

        if self.is_reversal != (self.reverses_id is not None):
            raise InvalidPosting(
                "A reversal names the row it reverses, and only a reversal does; got "
                f"is_reversal={self.is_reversal}, reverses={self.reverses_id!r}."
            )

        if self.account_id is not None and self.account.is_group:
            raise GroupAccountPosting(
                f"Account {self.account.code} ({self.account.name}) is a group and cannot "
                f"receive entries."
            )

    # ------------------------------------------------------------------
    # Convenience for reading, never for writing
    # ------------------------------------------------------------------
    @property
    def signed_paisa(self) -> int:
        """``debit - credit``. A raw figure; it carries no account sign."""
        return self.debit_paisa - self.credit_paisa
