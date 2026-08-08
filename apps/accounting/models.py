"""
Chart of accounts, the append-only general ledger, and the append-only stock
ledger that sits beside it.

:class:`LedgerEntry` is the source of truth for every figure this system will
ever print, and :class:`StockEntry` is the source of truth for every quantity
and every rupee of inventory value. Nothing else is. There is no cached balance
field on an account, on a party, on an item or on a document header, and there
must never be one (CLAUDE.md §6) — a cached total is a number that can disagree
with the ledger, and once one has disagreed for a week nobody can tell you which
of the two is right.

The rows are therefore append-only (CLAUDE.md §3): written once, never updated,
never deleted. A correction is a new row that mirrors what it reverses.

The two ledgers are the same shape and are deliberately not the same in one
place: a ledger row is a non-negative amount on one of two sides, while a stock
row is a **signed** quantity and a **signed** value. Money has debits and
credits, which are directions with names; stock has in and out, which are the
same direction with a sign. Forcing stock into two columns would mean every
balance query summing two fields and subtracting, and every valuation doing it
again.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import models
from simple_history.models import HistoricalRecords

from apps.core.exceptions import AppendOnlyViolation
from apps.core.fields import MoneyField, QuantityField
from apps.core.models import AppendOnlyModel, TimeStampedModel

from .enums import AccountType, PartyType, account_sign
from .exceptions import (
    GroupAccountPosting,
    InactiveAccount,
    InvalidAccount,
    InvalidPosting,
    InvalidWarehouse,
)


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

    #: An account is a **master**, and the only one outside apps.masters — which
    #: is why the history lives here and why the two ledgers next door do not
    #: have one. Every entry ever posted is filed under this row's ``type`` and
    #: its place in the tree, so "when did 5420 stop being a child of 5400" is a
    #: question with real money behind it. The ledger cannot answer it: the rows
    #: only hold the account id. See apps/masters/models.py for why documents
    #: are deliberately not registered.
    history = HistoricalRecords()

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


# ===========================================================================
# Stock
# ===========================================================================
class Warehouse(TimeStampedModel):
    """Somewhere stock is held: the shop, the godown, a delivery van.

    A master record like :class:`Account` — editable, not append-only — and
    like an account it is the thing entries hang off, so ``PROTECT`` on the
    stock ledger's foreign key means one that has movement cannot be deleted.

    Valuation is per ``(item, warehouse)``, never per item alone. Two
    warehouses that received the same item at different costs hold it at
    different rates, and averaging across them would value a van's stock at the
    godown's cost the moment either one was topped up.
    """

    code = models.CharField(
        max_length=16,
        unique=True,
        help_text="Stable identifier, e.g. MAIN. Sorting by code gives report order.",
    )
    name = models.CharField(max_length=128)
    is_default = models.BooleanField(
        default=False,
        help_text="The warehouse a document uses when it does not name one. At most one.",
    )

    class Meta:
        ordering = ["code"]
        verbose_name = "warehouse"
        verbose_name_plural = "warehouses"
        constraints = [
            # Partial unique index: many rows may be False, only one may be
            # True. Without it "the default warehouse" is whichever row the
            # database felt like returning first, and stock lands somewhere
            # nobody chose.
            models.UniqueConstraint(
                fields=["is_default"],
                condition=models.Q(is_default=True),
                name="warehouse_one_default",
                violation_error_message="Another warehouse is already the default.",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.code} — {self.name}"

    @classmethod
    def get_default(cls) -> Warehouse:
        """The warehouse flagged ``is_default``.

        Raises rather than returning ``None``: a caller that reached for the
        default has no second plan, and a silent ``None`` becomes a stock row
        with no warehouse a few frames later.
        """
        warehouse = cls.objects.filter(is_default=True).first()
        if warehouse is None:
            raise InvalidWarehouse(
                "No warehouse is marked as the default. Flag one, or name the warehouse "
                "explicitly on the document."
            )
        return warehouse

    def _assert_single_default(self) -> None:
        """The constraint above, raised in Python first so it reads as a sentence."""
        if not self.is_default:
            return
        clash = Warehouse.objects.filter(is_default=True).exclude(pk=self.pk).first()
        if clash is not None:
            raise InvalidWarehouse(
                f"{clash.code} ({clash.name}) is already the default warehouse. "
                f"Clear that flag before setting it here — there is exactly one default."
            )

    def clean(self):
        """Surface :meth:`_assert_single_default` as a form error in the admin."""
        super().clean()
        try:
            self._assert_single_default()
        except InvalidWarehouse as exc:
            raise ValidationError(str(exc)) from exc

    def save(self, *args, **kwargs):
        self._assert_single_default()
        return super().save(*args, **kwargs)


class StockEntryQuerySet(models.QuerySet):
    """The same doors :class:`LedgerEntryQuerySet` closes, on the stock ledger.

    Same reasoning: ``AppendOnlyModel`` guards the routes a person takes
    deliberately, and the bulk routes are the ones taken by accident.
    """

    def update(self, **kwargs):
        raise AppendOnlyViolation(
            "StockEntry is append-only; QuerySet.update() would rewrite posted history. "
            "Post a reversing entry with accounting.services.reverse_stock() instead."
        )

    def delete(self):
        raise AppendOnlyViolation(
            "StockEntry is append-only; QuerySet.delete() would erase posted history. "
            "Post a reversing entry with accounting.services.reverse_stock() instead."
        )


class StockEntryManager(models.Manager.from_queryset(StockEntryQuerySet)):
    """Manager for :class:`StockEntry`. Inserts only."""

    def bulk_update(self, *args, **kwargs):
        raise AppendOnlyViolation("StockEntry is append-only; bulk_update() is not available.")

    def bulk_create(self, objs, *args, **kwargs):
        # post_stock writes through here. update_conflicts turns the INSERT
        # into an UPSERT, which is an UPDATE wearing a hat.
        if kwargs.get("update_conflicts"):
            raise AppendOnlyViolation(
                "StockEntry is append-only; bulk_create(update_conflicts=True) would "
                "rewrite existing rows."
            )
        return super().bulk_create(objs, *args, **kwargs)


class StockEntry(AppendOnlyModel):
    """One item moving into or out of one warehouse. Written once, never changed.

    Signed, unlike :class:`LedgerEntry`: ``qty_base`` is positive coming in and
    negative going out, and ``value_paisa`` carries the same sign. See the
    module docstring for why the two ledgers differ here.

    ``rate_paisa`` is the cost of one base unit **as it stood when this row was
    written** — what the goods cost on the way in, and the moving weighted
    average on the way out. It is stored rather than derived so that a stock
    card can be read back years later without replaying every movement before
    it, and so that a back-dated entry can never silently re-value what was
    already posted.

    Where rounding makes ``qty_base * rate_paisa`` disagree with
    ``value_paisa`` by a paisa or two, **value_paisa is the figure that counts**
    — it is what balances sum, and it is computed so that issuing a whole
    position empties it exactly. See :mod:`apps.accounting.valuation`.

    The link back to the document is soft — a type name, an id and a
    denormalised code, no foreign key — for the same reason the ledger's is:
    the row must outlive any of the dozen document models that write it. The
    item and the warehouse are *not* soft: a movement without an item is not a
    movement, and PROTECT is what stops one being deleted out from under its
    history.
    """

    objects = StockEntryManager()

    posting_date = models.DateField(
        db_index=True,
        help_text="The day this hits the stock card. Not the day the row was written.",
    )
    item = models.ForeignKey(
        "masters.Item",
        on_delete=models.PROTECT,
        related_name="stock_entries",
        help_text="PROTECT: an item with movement cannot be deleted out from under it.",
    )
    warehouse = models.ForeignKey(
        Warehouse,
        on_delete=models.PROTECT,
        related_name="stock_entries",
        help_text="PROTECT: a warehouse with movement cannot be deleted out from under it.",
    )

    qty_base = QuantityField(
        help_text="Signed base units: positive in, negative out. Never fractional.",
    )
    rate_paisa = MoneyField(
        non_negative=True,
        help_text="Cost of one base unit at the moment this row was written.",
    )
    value_paisa = MoneyField(
        help_text="Signed cost value moved. The figure balances are summed from.",
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

    class Meta:
        verbose_name = "stock entry"
        verbose_name_plural = "stock entries"
        ordering = ["posting_date", "id"]
        permissions = [
            # The per-person half of ``settings.ALLOW_NEGATIVE_STOCK``. A
            # negative position has no cost behind it to average, so every later
            # issue out of it is valued at the last rate known and the error
            # spreads quietly into cost of goods sold — which is why this is a
            # permission somebody is granted rather than a checkbox on a form.
            (
                "override_negative_stock",
                "Can issue stock a warehouse does not hold, taking it below zero",
            ),
        ]
        indexes = [
            # The valuation query: "this item, in this warehouse, up to this
            # date". Every balance, every rate and every posting reads it.
            models.Index(
                fields=["item", "warehouse", "posting_date"],
                name="stock_item_wh_date_idx",
            ),
            # An item across every warehouse — the stock position report. The
            # index above cannot serve it: warehouse sits between the two
            # columns being filtered.
            models.Index(fields=["item", "posting_date"], name="stock_item_date_idx"),
            # Finding a voucher's rows, which is what reversal does.
            models.Index(fields=["voucher_type", "voucher_id"], name="stock_voucher_idx"),
        ]
        constraints = [
            # A row that moves nothing is not a movement. It would sit in the
            # stock card claiming something happened and contribute nothing,
            # which is worse than not being there.
            models.CheckConstraint(
                name="stockentry_qty_is_not_zero",
                condition=~models.Q(qty_base=0),
                violation_error_message="A stock entry moves a non-zero quantity.",
            ),
            models.CheckConstraint(
                name="stockentry_rate_non_negative",
                condition=models.Q(rate_paisa__gte=0),
                violation_error_message="A cost rate is never negative.",
            ),
            # Quantity and value move together or the balance stops meaning
            # anything: no putting quantity in while taking value out.
            models.CheckConstraint(
                name="stockentry_value_follows_qty",
                condition=(
                    models.Q(qty_base__gt=0, value_paisa__gte=0)
                    | models.Q(qty_base__lt=0, value_paisa__lte=0)
                ),
                violation_error_message="Quantity and value must move in the same direction.",
            ),
            models.CheckConstraint(
                name="stockentry_reversal_points_at_something",
                condition=(
                    models.Q(is_reversal=True, reverses__isnull=False)
                    | models.Q(is_reversal=False, reverses__isnull=True)
                ),
                violation_error_message="A reversal names the row it reverses; nothing else does.",
            ),
            # The database's own answer to double reversal. reverse_stock
            # refuses it in Python with a readable error; this makes it
            # impossible even if two cancellations race.
            models.UniqueConstraint(
                fields=["reverses"],
                condition=models.Q(reverses__isnull=False),
                name="stockentry_one_reversal_per_row",
                violation_error_message="That entry has already been reversed.",
            ),
        ]

    def __str__(self) -> str:
        direction = "in" if self.qty_base > 0 else "out"
        return (
            f"{self.posting_date} item={self.item_id} wh={self.warehouse_id} "
            f"{direction} {abs(self.qty_base)} @ {self.rate_paisa} ({self.voucher_code})"
        )

    # ------------------------------------------------------------------
    # Append-only
    # ------------------------------------------------------------------
    def save(self, *args, **kwargs):
        """Validate an insert, then hand over to the append-only guard.

        Identical in shape to :meth:`LedgerEntry.save`, and identical in
        reasoning: validation is scoped to the insert, because telling someone
        editing posted history that their quantities are wrong invites them to
        fix the quantities and try again. The real answer is that the row is
        permanent.

        :meth:`assert_valid` does not run on the ``bulk_create`` path that
        ``post_stock`` uses. That path is covered twice over: the service
        computes every value itself, and the CHECK constraints above catch
        anything that gets past it.
        """
        if self.pk is None:
            self.assert_valid()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        """Always raises. A posted row is history.

        Spelled out over ``AppendOnlyModel``'s guard so the error names the
        voucher, and so removing the base class does not quietly remove it.
        """
        raise AppendOnlyViolation(
            f"StockEntry pk={self.pk} ({self.voucher_type} {self.voucher_code}) cannot be "
            f"deleted — the stock ledger is append-only. Reverse the voucher instead."
        )

    def assert_valid(self) -> None:
        """Everything the CHECK constraints enforce, raised in Python first."""
        if isinstance(self.qty_base, bool) or not isinstance(self.qty_base, int):
            raise InvalidPosting(
                f"qty_base must be whole base units as an int, got "
                f"{type(self.qty_base).__name__}: {self.qty_base!r}. There is no half a piece "
                f"(CLAUDE.md §2)."
            )
        if self.qty_base == 0:
            raise InvalidPosting(
                "qty_base is 0. A stock entry that moves nothing records that something "
                "happened while contributing nothing to the balance."
            )

        for name in ("rate_paisa", "value_paisa"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise InvalidPosting(
                    f"{name} must be whole paisa as an int, got {type(value).__name__}: {value!r}"
                )

        if self.rate_paisa < 0:
            raise InvalidPosting(
                f"rate_paisa is {self.rate_paisa}; a cost rate is never negative. "
                f"Direction lives on qty_base and value_paisa, not on the rate."
            )

        # Zero value is legal in both directions — free goods cost nothing and
        # are issued at nothing. What is not legal is the two disagreeing.
        if (self.qty_base > 0 and self.value_paisa < 0) or (
            self.qty_base < 0 and self.value_paisa > 0
        ):
            raise InvalidPosting(
                f"qty_base={self.qty_base} and value_paisa={self.value_paisa} disagree on "
                f"direction. Stock coming in carries value in; stock going out carries it out."
            )

        if self.is_reversal != (self.reverses_id is not None):
            raise InvalidPosting(
                "A reversal names the row it reverses, and only a reversal does; got "
                f"is_reversal={self.is_reversal}, reverses={self.reverses_id!r}."
            )

    # ------------------------------------------------------------------
    # Convenience for reading, never for writing
    # ------------------------------------------------------------------
    @property
    def is_inward(self) -> bool:
        """True for a receipt, False for an issue. Reads the sign, nothing else."""
        return self.qty_base > 0
