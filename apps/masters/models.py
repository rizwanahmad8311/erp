"""Items, categories, parties, routes and sellers — the rows every document
points a foreign key at.

None of this is append-only and none of it is a document. These are master
records: editable, correctable, and deliberately holding **no balances**. There
is no ``current_stock`` on an item and no ``outstanding`` on a client, and there
must never be one — every such figure is aggregated from the two ledgers in
:mod:`apps.accounting` (CLAUDE.md §6). The one number here that looks like a
balance, ``Party.opening_balance_paisa``, is not one; see the note on the field.

What masters *does* own is the packing arithmetic. An item knows how many pieces
are in its carton, and that is the only fact in the system that can turn "3
cartons" into the whole number of base units a stock row stores. The conversion
itself lives in :mod:`apps.masters.services` and nowhere else.

History
-------
Every model here carries ``HistoricalRecords()``, and that is deliberately the
*only* place in the system that does. A master is edited in place — somebody
corrects a phone number, raises a credit limit, changes a carton size — and the
row afterwards is the only record that the row before ever said anything else.
Without a history table, "who put this shop's limit up to Rs 400,000, and when"
has no answer.

Documents are the opposite and must **never** be registered: a POSTED document
cannot be modified at all (CLAUDE.md §5), and every correction is a reversing
entry that is already in the ledger under its own date and its own user
(CLAUDE.md §3). A second audit log over documents would be a second version of
the truth, and the two would eventually disagree.

The history rows are ordinary tables with ordinary foreign keys and they hold no
balances — a ``HistoricalClient`` has the same fields the client has, which is
the point.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from simple_history.models import HistoricalRecords

from apps.core.fields import MoneyField, QuantityField
from apps.core.models import TimeStampedModel

from .enums import BASIS_POINTS_PER_UNIT, DayOfWeek, Unit, day_order
from .exceptions import DuplicatePrimarySeller, InvalidCategory, InvalidPacking


# ===========================================================================
# Items
# ===========================================================================
class ItemCategory(TimeStampedModel):
    """A node in the item tree: Food > Snacks, Non-Food > Home Care.

    Shallow and small — a distribution business runs on a couple of dozen of
    these. Unlike :class:`~apps.accounting.Account` there is no group/leaf
    split, because a category is only ever a grouping: items hang off whichever
    node the operator picked, interior or not, and a report that wants the whole
    subtree walks ``children``.
    """

    name = models.CharField(max_length=128)
    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="children",
        help_text="The category this one sits under. Empty for a top-level category.",
    )

    class Meta:
        ordering = ["name"]
        verbose_name = "item category"
        verbose_name_plural = "item categories"
        constraints = [
            # Two siblings with the same name are indistinguishable in every
            # dropdown in the system. Split in two because SQLite treats NULLs
            # as distinct, so a single UniqueConstraint over (parent, name)
            # would let "Beverages" exist twice at the top level.
            models.UniqueConstraint(
                fields=["parent", "name"],
                condition=models.Q(parent__isnull=False),
                name="itemcategory_unique_name_per_parent",
                violation_error_message="That category already has a child with this name.",
            ),
            models.UniqueConstraint(
                fields=["name"],
                condition=models.Q(parent__isnull=True),
                name="itemcategory_unique_root_name",
                violation_error_message="A top-level category with this name already exists.",
            ),
        ]

    def __str__(self) -> str:
        # One level of context, not the whole path: this string appears in every
        # category dropdown, and walking to the root would be a query per row.
        if self.parent_id:
            return f"{self.parent.name} > {self.name}"
        return self.name

    def ancestors(self) -> list[ItemCategory]:
        """Root-last walk up the tree. Cheap: category trees are two deep."""
        chain: list[ItemCategory] = []
        seen = {self.pk}
        node = self
        while node.parent_id:
            node = node.parent
            if node.pk in seen:  # defensive; _assert_structure prevents cycles
                raise InvalidCategory(f"Category {self.name} sits in a parent cycle.")
            seen.add(node.pk)
            chain.append(node)
        return chain

    def _assert_structure(self) -> None:
        if self.parent_id is None:
            return
        if self.parent_id == self.pk:
            raise InvalidCategory(f"Category {self.name} cannot be its own parent.")
        if self.pk is not None and self.pk in {c.pk for c in self.parent.ancestors()}:
            raise InvalidCategory(
                f"Making {self.parent.name} the parent of {self.name} would create a cycle."
            )

    def clean(self):
        """Surface :meth:`_assert_structure` as a form error in the admin."""
        super().clean()
        try:
            self._assert_structure()
        except InvalidCategory as exc:
            raise ValidationError(str(exc)) from exc

    def save(self, *args, **kwargs):
        self._assert_structure()
        return super().save(*args, **kwargs)


class Item(TimeStampedModel):
    """A thing that is bought, held and sold, counted in whole base units.

    Every quantity recorded against this item — on an order line, on a stock
    row, in a report — is an integer number of ``base_unit`` (CLAUDE.md §2).
    ``carton_size`` is the *only* thing that makes "2 cartons" mean anything,
    and it means it exactly once, in
    :func:`apps.masters.services.to_base`.

    ``carton_size = 1`` is the normal state of an item that is not cartoned at
    all — a 25kg rice bag, a nappy pack. Such an item never displays a carton
    figure, which is why :attr:`allows_carton` exists and why nothing formats
    quantities by dividing on its own.

    The two rates are **defaults for data entry, per base unit**. They are not
    what anything is valued at: purchase cost comes off the goods receipt and
    inventory value is the moving average held on the stock ledger. Changing a
    rate here changes what the next document is pre-filled with, and nothing
    that has already been posted.
    """

    code = models.CharField(
        max_length=32,
        unique=True,
        help_text="Stable identifier used on documents and in stock reports.",
    )
    name = models.CharField(max_length=200)
    category = models.ForeignKey(
        ItemCategory,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="items",
        help_text="Optional grouping for reports and for finding things in a long list.",
    )

    base_unit = models.CharField(
        max_length=8,
        choices=Unit.choices,
        default=Unit.PIECE,
        help_text=(
            "The unit one stored quantity of 1 means. Almost always PIECE. "
            "CARTON is for goods only ever moved as a sealed carton, and forces "
            "carton_size to 1."
        ),
    )
    carton_size = QuantityField(
        default=1,
        validators=[MinValueValidator(1)],
        help_text="Base units in one carton. 1 means the item is not sold by the carton.",
    )

    purchase_rate_paisa = MoneyField(
        non_negative=True,
        help_text="Default buying rate for ONE BASE UNIT, in paisa. A data-entry default only.",
    )
    sale_rate_paisa = MoneyField(
        non_negative=True,
        help_text="Default selling rate for ONE BASE UNIT, in paisa. A data-entry default only.",
    )

    tax_rate_bp = models.PositiveIntegerField(
        default=0,
        validators=[MaxValueValidator(BASIS_POINTS_PER_UNIT)],
        help_text="Sales tax in basis points: 1750 is 17.5%, 0 is exempt.",
    )

    is_active = models.BooleanField(
        default=True,
        help_text="Deactivating keeps the item off new documents. It never hides history.",
    )

    #: Rate and packing changes are the ones that get argued about later — a
    #: carton size edited after a month of receipts changes what every quantity
    #: on the screen means.
    history = HistoricalRecords()

    class Meta:
        ordering = ["code"]
        verbose_name = "item"
        verbose_name_plural = "items"
        indexes = [
            models.Index(fields=["name"], name="item_name_idx"),
        ]
        constraints = [
            # A carton of zero pieces is a division by zero in from_base, and a
            # negative one inverts every conversion silently.
            models.CheckConstraint(
                name="item_carton_size_at_least_one",
                condition=models.Q(carton_size__gte=1),
                violation_error_message="A carton holds at least one base unit.",
            ),
            models.CheckConstraint(
                name="item_tax_rate_within_range",
                condition=models.Q(tax_rate_bp__gte=0, tax_rate_bp__lte=BASIS_POINTS_PER_UNIT),
                violation_error_message="Tax rate is between 0 and 10000 basis points.",
            ),
            # If the carton IS the base unit then "pieces per carton" is one,
            # by definition. Anything else describes a packing level that has no
            # unit to be counted in.
            models.CheckConstraint(
                name="item_carton_base_unit_holds_one",
                condition=~models.Q(base_unit=Unit.CARTON) | models.Q(carton_size=1),
                violation_error_message=(
                    "An item counted in cartons has a carton_size of 1 — the carton is "
                    "already the base unit."
                ),
            ),
        ]

    def __str__(self) -> str:
        return f"{self.code} — {self.name}"

    # ------------------------------------------------------------------
    # Packing
    # ------------------------------------------------------------------
    @property
    def allows_carton(self) -> bool:
        """True when this item can be entered and shown by the carton.

        The single question every quantity widget and every printed line should
        ask. An item with ``carton_size = 1`` is not cartoned, and formatting
        one as "17 ctn + 0 pcs" is how a picker ends up loading the wrong thing.
        """
        return self.carton_size > 1

    @property
    def tax_rate_display(self) -> str:
        """``1750`` -> ``"17.50%"``. Display only.

        Integer arithmetic on purpose. A basis-point rate is exact as an int and
        rendering it does not need — and must not introduce — a second rounding
        site (CLAUDE.md §1).
        """
        whole, hundredths = divmod(self.tax_rate_bp, 100)
        return f"{whole}.{hundredths:02d}%"

    def _assert_packing(self) -> None:
        """The packing CHECK constraints, raised in Python first.

        The constraints are the real guarantee; this exists so a mistake fails
        with a sentence rather than with an ``IntegrityError`` naming an index.
        """
        if isinstance(self.carton_size, bool) or not isinstance(self.carton_size, int):
            raise InvalidPacking(
                f"carton_size must be a whole number of base units, got "
                f"{type(self.carton_size).__name__}: {self.carton_size!r}"
            )
        if self.carton_size < 1:
            raise InvalidPacking(
                f"Item {self.code} has carton_size={self.carton_size}. A carton holds at "
                f"least one base unit; use 1 for an item that is not sold by the carton."
            )
        if self.base_unit == Unit.CARTON and self.carton_size != 1:
            raise InvalidPacking(
                f"Item {self.code} is counted in cartons, so carton_size must be 1 — the "
                f"carton is already the base unit, and {self.carton_size} describes a "
                f"packing level with no unit to count it in."
            )

    def clean(self):
        """Surface :meth:`_assert_packing` as a form error in the admin."""
        super().clean()
        try:
            self._assert_packing()
        except InvalidPacking as exc:
            raise ValidationError(str(exc)) from exc

    def save(self, *args, **kwargs):
        self._assert_packing()
        return super().save(*args, **kwargs)


# ===========================================================================
# Routes and sellers
# ===========================================================================
class Route(TimeStampedModel):
    """A delivery beat: the shops one van covers on one day.

    The day is what makes this a schedule rather than a grouping, and it is
    nullable because a route may genuinely be unscheduled — a spot run, or a new
    area that has not been given a slot yet.
    """

    code = models.CharField(
        max_length=16,
        unique=True,
        help_text="Stable identifier, e.g. R-MON. Sorting by code gives report order.",
    )
    name = models.CharField(max_length=128)

    # NULL rather than "" on purpose, against DJ001's usual advice. That rule
    # exists to stop a field having two empty values; the CHECK below forbids ""
    # outright, so NULL is the only way to say "unscheduled" and every query for
    # one is `day_of_week__isnull=True`.
    day_of_week = models.CharField(  # noqa: DJ001
        max_length=3,
        choices=DayOfWeek.choices,
        null=True,
        blank=True,
        db_index=True,
        help_text="The day this route is run. Empty for an unscheduled route.",
    )

    is_active = models.BooleanField(
        default=True,
        help_text="Deactivating keeps the route off new orders. Its clients are untouched.",
    )

    #: A route that changes day changes whose round a shop is on, which is what
    #: a recovery sheet is ordered by.
    history = HistoricalRecords()

    class Meta:
        ordering = ["code"]
        verbose_name = "route"
        verbose_name_plural = "routes"
        constraints = [
            models.CheckConstraint(
                name="route_day_is_null_not_blank",
                condition=~models.Q(day_of_week=""),
                violation_error_message="An unscheduled route has no day at all, not a blank one.",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.code} — {self.name}"

    @property
    def client_count(self) -> int:
        """How many clients sit on this route.

        Uses an annotation named ``_client_count`` when the queryset supplied
        one — which the admin changelist does — so a page of routes is one query
        rather than one per row. Falls back to counting, so the property is
        always correct wherever it is read from.
        """
        annotated = getattr(self, "_client_count", None)
        if annotated is not None:
            return annotated
        return self.clients.count()

    @property
    def day_index(self) -> int:
        """Position in the week, for sorting a schedule. Unscheduled sorts last."""
        return day_order(self.day_of_week)


class Seller(TimeStampedModel):
    """An order booker: the person who walks a route and writes the orders.

    Not a Django user. A seller is a party to the business — commissions are
    calculated against them and recovery is chased through them — and most of
    them will never log in to anything.
    """

    code = models.CharField(
        max_length=16,
        unique=True,
        help_text="Stable identifier, e.g. S-01. Appears on order books and commission reports.",
    )
    name = models.CharField(max_length=128)
    phone = models.CharField(max_length=32, blank=True, default="")
    is_active = models.BooleanField(
        default=True,
        help_text="Deactivating keeps the seller off new orders. It never hides history.",
    )

    routes = models.ManyToManyField(
        Route,
        through="RouteSeller",
        related_name="sellers",
        blank=True,
        help_text="The routes this seller works. One of them may be marked primary.",
    )

    #: Commission is calculated against a seller, so who they were is a question
    #: that gets asked about a period rather than about today.
    history = HistoricalRecords()

    class Meta:
        ordering = ["code"]
        verbose_name = "seller"
        verbose_name_plural = "sellers"

    def __str__(self) -> str:
        return f"{self.code} — {self.name}"


class RouteSeller(TimeStampedModel):
    """Which sellers work which routes, and who owns each one.

    Many-to-many in both directions and genuinely so: a busy route carries a
    second booker, and a seller covers a different area on a different day.
    ``is_primary`` is what breaks the tie when something has to name one person
    for a route — at most one per route, enforced by a partial unique index the
    same way the default warehouse is.

    ``CASCADE`` on both sides, unlike the ``PROTECT`` used everywhere else in
    masters: this row is a link and nothing else. It carries no history, so
    removing a seller from a route should remove the link rather than refuse.
    What is protected is the thing that matters — a route with clients on it
    cannot be deleted, because :attr:`Client.route` is ``PROTECT``.
    """

    route = models.ForeignKey(Route, on_delete=models.CASCADE, related_name="route_sellers")
    seller = models.ForeignKey(Seller, on_delete=models.CASCADE, related_name="route_sellers")
    is_primary = models.BooleanField(
        default=False,
        help_text="The seller this route belongs to. At most one per route.",
    )

    class Meta:
        ordering = ["route__code", "-is_primary", "seller__code"]
        verbose_name = "route seller"
        verbose_name_plural = "route sellers"
        constraints = [
            models.UniqueConstraint(
                fields=["route", "seller"],
                name="routeseller_unique_pair",
                violation_error_message="That seller is already on this route.",
            ),
            # Partial unique index: many rows may be False, only one may be True
            # per route. Without it "the route's seller" is whichever row the
            # database returned first.
            models.UniqueConstraint(
                fields=["route"],
                condition=models.Q(is_primary=True),
                name="routeseller_one_primary_per_route",
                violation_error_message="This route already has a primary seller.",
            ),
        ]

    def __str__(self) -> str:
        suffix = " (primary)" if self.is_primary else ""
        return f"{self.route_id} / {self.seller_id}{suffix}"

    def _assert_single_primary(self) -> None:
        """The constraint above, raised in Python first so it reads as a sentence."""
        if not self.is_primary:
            return
        clash = (
            RouteSeller.objects.filter(route_id=self.route_id, is_primary=True)
            .exclude(pk=self.pk)
            .select_related("seller")
            .first()
        )
        if clash is not None:
            raise DuplicatePrimarySeller(
                f"{clash.seller.name} is already the primary seller on this route. "
                f"Clear that flag before setting it here — there is exactly one primary."
            )

    def clean(self):
        """Surface :meth:`_assert_single_primary` as a form error in the admin."""
        super().clean()
        try:
            self._assert_single_primary()
        except DuplicatePrimarySeller as exc:
            raise ValidationError(str(exc)) from exc

    def save(self, *args, **kwargs):
        self._assert_single_primary()
        return super().save(*args, **kwargs)


# ===========================================================================
# Parties
# ===========================================================================
class Party(TimeStampedModel):
    """Everything a client and a vendor have in common.

    Abstract, and two concrete tables rather than one table with a type column.
    They are asked completely different questions — a client has a route and a
    booker and gets chased for recovery; a vendor has none of those and is paid
    — and merging them means every query in the system carries a type filter it
    can forget.

    There is **no balance field here**, and adding one would break CLAUDE.md §6.
    A party's balance is ``sum(debits) - sum(credits)`` over the ledger rows
    whose soft party link points at them, and that sum is the only figure that
    can be trusted.
    """

    code = models.CharField(
        max_length=16,
        unique=True,
        help_text="Stable identifier, e.g. C-0001. Appears on every document and statement.",
    )
    name = models.CharField(max_length=128)
    phone = models.CharField(max_length=32, blank=True, default="")
    address = models.TextField(blank=True, default="")
    city = models.CharField(max_length=64, blank=True, default="", db_index=True)

    # Signed, unlike a credit limit: a party may open in either direction — a
    # client who paid an advance before go-live is in credit.
    #
    # This is NOT a balance and nothing reads it as one. It is the figure the
    # operator typed at go-live, kept so the opening journal voucher can be
    # posted from it and so it can be checked against later. The party's actual
    # balance is aggregated from the ledger, always (CLAUDE.md §6).
    opening_balance_paisa = MoneyField(
        help_text=(
            "What was owed at go-live, in paisa. Positive means the normal direction "
            "of business (a client owes us, we owe a vendor). Data entry only — the "
            "live balance is aggregated from the ledger."
        ),
    )
    credit_limit_paisa = MoneyField(
        non_negative=True,
        help_text="Maximum outstanding allowed, in paisa. 0 means no credit is extended.",
    )
    credit_days = models.PositiveIntegerField(
        default=0,
        help_text="Days before an invoice is overdue. 0 means cash on delivery.",
    )

    is_active = models.BooleanField(
        default=True,
        help_text="Deactivating keeps the party off new documents. It never hides history.",
    )

    class Meta:
        abstract = True
        ordering = ["code"]

    def __str__(self) -> str:
        return f"{self.code} — {self.name}"


class Client(Party):
    """A shop that buys from us.

    Carries the two things a vendor does not: the route it is delivered on and
    the seller who usually books it. Both are ``PROTECT``, so a route or a
    seller with shops attached cannot be deleted out from under them, and both
    are optional — a walk-in cash customer belongs to no route.

    The seller here is the *usual* booker, not a restriction. Someone covering a
    sick colleague books the order under their own name and the order records
    that; this field is what the order form pre-fills from.
    """

    route = models.ForeignKey(
        Route,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="clients",
        help_text="The beat this shop is delivered on. Empty for a walk-in.",
    )
    seller = models.ForeignKey(
        Seller,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="clients",
        help_text="The order booker who usually serves this shop. A default, not a restriction.",
    )

    #: The credit limit is the field this history exists for: raising one is a
    #: decision somebody made on a day, and the ledger has no record of it.
    history = HistoricalRecords()

    class Meta(Party.Meta):
        verbose_name = "client"
        verbose_name_plural = "clients"


class Vendor(Party):
    """A supplier we buy from. Party fields and nothing else.

    ``credit_days`` reads the other way round here — it is how long *we* have to
    pay — which is the whole reason ``Party`` holds the field rather than each
    subclass naming it for its own side.
    """

    history = HistoricalRecords()

    class Meta(Party.Meta):
        verbose_name = "vendor"
        verbose_name_plural = "vendors"
