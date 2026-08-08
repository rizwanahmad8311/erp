"""Unfold-styled admin for the master data.

Register with ``unfold.admin.ModelAdmin``, not ``django.contrib.admin.ModelAdmin``.

Everything here is editable — masters are corrected in place, unlike the ledgers
next door. What stops a correction destroying history is the foreign keys:
``StockEntry.item``, ``Client.route`` and ``Client.seller`` are all ``PROTECT``,
so anything with movement or shops attached refuses to be deleted no matter what
is clicked on this page.

Money is rendered through ``apps.core.money.fmt`` and quantities through
``apps.masters.services.fmt_qty``. Neither is ever computed inline here: the
admin displays, it does not do arithmetic.
"""

from django.contrib import admin
from django.db.models import Count
from unfold.admin import ModelAdmin, TabularInline

from apps.core.money import fmt

from .models import Client, Item, ItemCategory, Route, RouteSeller, Seller, Vendor

#: Repeated on every admin below. Audit stamps are written by the framework and
#: are never typed in.
AUDIT_FIELDS = ("created_at", "created_by", "updated_at", "updated_by")
AUDIT_FIELDSET = ("Audit", {"fields": AUDIT_FIELDS, "classes": ["collapse"]})


# ===========================================================================
# Items
# ===========================================================================
@admin.register(ItemCategory)
class ItemCategoryAdmin(ModelAdmin):
    """The item tree. Small, shallow, and edited a handful of times a year."""

    list_display = ("name", "parent", "item_count")
    list_filter = ("parent",)
    search_fields = ("name", "parent__name")
    ordering = ("name",)
    list_select_related = ("parent",)
    autocomplete_fields = ("parent",)
    readonly_fields = AUDIT_FIELDS
    fieldsets = (
        (None, {"fields": ("name", "parent")}),
        AUDIT_FIELDSET,
    )

    def get_queryset(self, request):
        # Annotated rather than counted per row: a changelist of 30 categories
        # would otherwise be 31 queries.
        return super().get_queryset(request).annotate(_item_count=Count("items"))

    @admin.display(description="Items", ordering="_item_count")
    def item_count(self, obj) -> int:
        return obj._item_count


@admin.register(Item)
class ItemAdmin(ModelAdmin):
    """The item master, with packing and the two default rates on one screen.

    Deleting an item that has stock movement is impossible regardless of what is
    clicked here: ``StockEntry.item`` is ``PROTECT``.
    """

    list_display = (
        "code",
        "name",
        "category",
        "packing",
        "purchase_rate",
        "sale_rate",
        "tax_rate",
        "is_active",
    )
    list_filter = ("is_active", "base_unit", "category")
    search_fields = ("code", "name", "category__name")
    ordering = ("code",)
    list_select_related = ("category",)
    autocomplete_fields = ("category",)
    list_per_page = 50
    readonly_fields = AUDIT_FIELDS
    fieldsets = (
        (None, {"fields": ("code", "name", "category")}),
        (
            "Packing",
            {
                "fields": ("base_unit", "carton_size"),
                "description": (
                    "Quantities are stored in whole base units. carton_size is how many "
                    "of them are in one carton — leave it at 1 for an item that is not "
                    "sold by the carton."
                ),
            },
        ),
        (
            "Default rates (per base unit)",
            {
                "fields": ("purchase_rate_paisa", "sale_rate_paisa", "tax_rate_bp"),
                "description": (
                    "Entered in paisa: 12550 is Rs 125.50. These pre-fill new document "
                    "lines and are never what posted stock is valued at."
                ),
            },
        ),
        ("Status", {"fields": ("is_active",)}),
        AUDIT_FIELDSET,
    )

    @admin.display(description="Packing", ordering="carton_size")
    def packing(self, obj) -> str:
        if not obj.allows_carton:
            return f"loose ({obj.get_base_unit_display().lower()})"
        return f"{obj.carton_size} per carton"

    @admin.display(description="Purchase", ordering="purchase_rate_paisa")
    def purchase_rate(self, obj) -> str:
        return fmt(obj.purchase_rate_paisa)

    @admin.display(description="Sale", ordering="sale_rate_paisa")
    def sale_rate(self, obj) -> str:
        return fmt(obj.sale_rate_paisa)

    @admin.display(description="Tax", ordering="tax_rate_bp")
    def tax_rate(self, obj) -> str:
        return obj.tax_rate_display


# ===========================================================================
# Routes and sellers
# ===========================================================================
class RouteSellerInline(TabularInline):
    """The route/seller link. Subclassed per side so each screen only shows the
    other end of the link — the route is already known on a route page."""

    model = RouteSeller
    extra = 0
    verbose_name = "route/seller link"
    verbose_name_plural = "route/seller links"


class RouteSellerByRouteInline(RouteSellerInline):
    """Shown on a route: which sellers work it."""

    fields = ("seller", "is_primary")
    autocomplete_fields = ("seller",)


class RouteSellerBySellerInline(RouteSellerInline):
    """Shown on a seller: which routes they work."""

    fields = ("route", "is_primary")
    autocomplete_fields = ("route",)


@admin.register(Route)
class RouteAdmin(ModelAdmin):
    """The delivery beats, with the shop count that decides whether one needs splitting."""

    list_display = ("code", "name", "day", "client_count", "seller_names", "is_active")
    list_filter = ("is_active", "day_of_week")
    search_fields = ("code", "name")
    ordering = ("code",)
    inlines = (RouteSellerByRouteInline,)
    readonly_fields = AUDIT_FIELDS
    fieldsets = (
        (None, {"fields": ("code", "name")}),
        (
            "Schedule",
            {
                "fields": ("day_of_week",),
                "description": "Leave empty for an unscheduled route — a spot run or a new area.",
            },
        ),
        ("Status", {"fields": ("is_active",)}),
        AUDIT_FIELDSET,
    )

    def get_queryset(self, request):
        # ``Route.client_count`` reads this annotation when it is present, so a
        # page of routes costs one query instead of one per row.
        return (
            super()
            .get_queryset(request)
            .annotate(_client_count=Count("clients", distinct=True))
            .prefetch_related("route_sellers__seller")
        )

    @admin.display(description="Day", ordering="day_of_week")
    def day(self, obj) -> str:
        return obj.get_day_of_week_display() or "—"

    @admin.display(description="Clients", ordering="_client_count")
    def client_count(self, obj) -> int:
        return obj.client_count

    @admin.display(description="Sellers")
    def seller_names(self, obj) -> str:
        # Prefetched above, so this is free. Primary first, marked.
        links = sorted(obj.route_sellers.all(), key=lambda rs: (not rs.is_primary, rs.seller.code))
        return ", ".join(f"{rs.seller.name}{'*' if rs.is_primary else ''}" for rs in links) or "—"


@admin.register(Seller)
class SellerAdmin(ModelAdmin):
    """The order bookers. Not Django users — most of them never log in."""

    list_display = ("code", "name", "phone", "route_names", "client_count", "is_active")
    list_filter = ("is_active", "routes")
    search_fields = ("code", "name", "phone")
    ordering = ("code",)
    inlines = (RouteSellerBySellerInline,)
    readonly_fields = AUDIT_FIELDS
    fieldsets = (
        (None, {"fields": ("code", "name", "phone")}),
        ("Status", {"fields": ("is_active",)}),
        AUDIT_FIELDSET,
    )

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .annotate(_client_count=Count("clients", distinct=True))
            .prefetch_related("route_sellers__route")
        )

    @admin.display(description="Routes")
    def route_names(self, obj) -> str:
        links = sorted(obj.route_sellers.all(), key=lambda rs: (not rs.is_primary, rs.route.code))
        return ", ".join(f"{rs.route.code}{'*' if rs.is_primary else ''}" for rs in links) or "—"

    @admin.display(description="Clients", ordering="_client_count")
    def client_count(self, obj) -> int:
        return obj._client_count


@admin.register(RouteSeller)
class RouteSellerAdmin(ModelAdmin):
    """The link table on its own page, for "who covers what" across every route.

    The inlines on Route and Seller are the everyday way in; this exists because
    a question like "which routes have no primary seller" is a filter here and a
    lot of clicking anywhere else.
    """

    list_display = ("route", "seller", "is_primary")
    list_filter = ("is_primary", "route", "seller")
    search_fields = ("route__code", "route__name", "seller__code", "seller__name")
    ordering = ("route__code", "-is_primary")
    list_select_related = ("route", "seller")
    autocomplete_fields = ("route", "seller")
    readonly_fields = AUDIT_FIELDS
    fieldsets = (
        (None, {"fields": ("route", "seller", "is_primary")}),
        AUDIT_FIELDSET,
    )


# ===========================================================================
# Parties
# ===========================================================================
class PartyAdminMixin:
    """The columns and fieldsets a client and a vendor share.

    Split out rather than repeated, because the two party admins drifting apart
    is how "credit limit" ends up meaning something different on each screen.
    """

    search_fields = ("code", "name", "phone", "city")
    ordering = ("code",)
    list_per_page = 50
    readonly_fields = AUDIT_FIELDS

    @admin.display(description="Opening", ordering="opening_balance_paisa")
    def opening_balance(self, obj) -> str:
        return fmt(obj.opening_balance_paisa)

    @admin.display(description="Credit limit", ordering="credit_limit_paisa")
    def credit_limit(self, obj) -> str:
        return fmt(obj.credit_limit_paisa) if obj.credit_limit_paisa else "cash only"

    @admin.display(description="Terms", ordering="credit_days")
    def terms(self, obj) -> str:
        return f"{obj.credit_days} days" if obj.credit_days else "COD"


#: Shared by both party admins. The "Credit" description is the one thing on
#: these screens that is easy to misread, so it says what the numbers are not.
CREDIT_FIELDSET = (
    "Credit",
    {
        "fields": ("opening_balance_paisa", "credit_limit_paisa", "credit_days"),
        "description": (
            "Entered in paisa: 12550 is Rs 125.50. The opening balance is what was "
            "owed at go-live and is data entry only — the live balance is always "
            "aggregated from the ledger, never read from here."
        ),
    },
)
CONTACT_FIELDSET = (
    "Contact",
    {"fields": ("phone", "city", "address")},
)


@admin.register(Client)
class ClientAdmin(PartyAdminMixin, ModelAdmin):
    """The shops. Filterable by route and by seller, which is how a day's work is found."""

    list_display = (
        "code",
        "name",
        "city",
        "phone",
        "route",
        "seller",
        "credit_limit",
        "terms",
        "is_active",
    )
    list_filter = ("is_active", "route", "seller", "city")
    search_fields = (*PartyAdminMixin.search_fields, "route__code", "route__name", "seller__name")
    list_select_related = ("route", "seller")
    autocomplete_fields = ("route", "seller")
    fieldsets = (
        (None, {"fields": ("code", "name")}),
        CONTACT_FIELDSET,
        (
            "Beat",
            {
                "fields": ("route", "seller"),
                "description": "Both optional — a walk-in cash customer belongs to no route.",
            },
        ),
        CREDIT_FIELDSET,
        ("Status", {"fields": ("is_active",)}),
        AUDIT_FIELDSET,
    )


@admin.register(Vendor)
class VendorAdmin(PartyAdminMixin, ModelAdmin):
    """The suppliers. Same shape as a client, minus the beat.

    ``credit_days`` reads the other way round here: it is how long we have to
    pay them.
    """

    list_display = ("code", "name", "city", "phone", "credit_limit", "terms", "is_active")
    list_filter = ("is_active", "city")
    fieldsets = (
        (None, {"fields": ("code", "name")}),
        CONTACT_FIELDSET,
        CREDIT_FIELDSET,
        ("Status", {"fields": ("is_active",)}),
        AUDIT_FIELDSET,
    )
