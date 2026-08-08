"""The five roles this business has, as Django groups.

A person's access is their group. Nobody is given a permission directly — the
admin allows it, and it will happen, but the five groups below are what the
office is described in and what the tests assert against. If somebody needs
something a group does not have, the honest fix is to change the group here and
write a migration, because the next person hired into that role needs it too.

The roles, and the one sentence each of them is:

``Admin``       everything, including the backup and the user list.
``Accountant``  the books: cancel, amend, reverse, reconcile, and every report.
``Operator``    the counter: write bills and post them. Never reverses one.
``Booker``      the round: their own routes' shops, and the money they collect.
``Viewer``      reads. No cost prices, no financial statements.

Seeding is **additive and idempotent**, like
:func:`apps.accounting.chart.seed_chart_of_accounts` and for the same reason: a
live installation will have tuned a group, and a seed that "corrected" that
would be a support call. A missing group is created with its full set; an
existing group has missing permissions added and **nothing taken away**.
Removing a permission from a group is a policy decision, and it should look like
one — a migration somebody wrote on purpose.

Nothing here ever touches which users are in which group.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import permissions as perms


# ---------------------------------------------------------------------------
# The building blocks
# ---------------------------------------------------------------------------
#: Everything a document type's screens need to *write* one, short of the
#: lifecycle actions. ``delete`` is in here and is not as alarming as it looks:
#: :meth:`apps.core.models.DocumentModel.delete` refuses on anything but a
#: DRAFT, which has written nothing to any ledger (CLAUDE.md §5).
def _crud(app_label: str, *model_names: str) -> list[str]:
    return [
        f"{app_label}.{action}_{model_name}"
        for model_name in model_names
        for action in ("view", "add", "change", "delete")
    ]


def _view(app_label: str, *model_names: str) -> list[str]:
    return [f"{app_label}.view_{model_name}" for model_name in model_names]


def _lifecycle(*, post=(), cancel=(), amend=()) -> list[str]:
    """Named lifecycle permissions, per ``(app_label, model_name)`` pair."""
    out = []
    for app_label, model_name in post:
        out.append(f"{app_label}.post_{model_name}")
    for app_label, model_name in cancel:
        out.append(f"{app_label}.cancel_{model_name}")
    for app_label, model_name in amend:
        out.append(f"{app_label}.amend_{model_name}")
    return out


SALES_DOCUMENTS = (("sales", "salesinvoice"), ("sales", "salesreturn"))
PURCHASE_DOCUMENTS = (("purchasing", "purchaseinvoice"), ("purchasing", "purchasereturn"))
PAYMENT_DOCUMENTS = (("payments", "payment"), ("payments", "chequeevent"))

SALES_MODELS = ("salesinvoice", "salesreturn", "salesinvoiceline", "salesreturnline")
PURCHASE_MODELS = (
    "purchaseinvoice",
    "purchasereturn",
    "purchaseinvoiceline",
    "purchasereturnline",
)
PAYMENT_MODELS = ("payment", "chequeevent", "paymentallocation")
MASTER_MODELS = ("item", "itemcategory", "client", "vendor", "route", "seller", "routeseller")
ACCOUNTING_MODELS = ("account", "warehouse", "ledgerentry", "stockentry")


@dataclass(frozen=True)
class GroupSpec:
    """One role: its name, why it exists, and exactly what it may do."""

    name: str
    description: str
    permissions: list[str] = field(default_factory=list)
    #: Whether this group simply holds every permission there is. Only ``Admin``
    #: does, and it is a flag rather than a list so that a capability added next
    #: year is one the administrator has without anybody remembering.
    everything: bool = False


# ---------------------------------------------------------------------------
# The roles
# ---------------------------------------------------------------------------
ADMIN = GroupSpec(
    name="Admin",
    description="Everything, including the backup and the user list.",
    everything=True,
)

ACCOUNTANT = GroupSpec(
    name="Accountant",
    description=(
        "The books. Cancels and amends, reconciles cheques, and reads every report "
        "including the financial statements."
    ),
    permissions=[
        *_crud("sales", *SALES_MODELS),
        *_crud("purchasing", *PURCHASE_MODELS),
        *_crud("payments", *PAYMENT_MODELS),
        *_lifecycle(
            post=(*SALES_DOCUMENTS, *PURCHASE_DOCUMENTS, *PAYMENT_DOCUMENTS),
            cancel=(*SALES_DOCUMENTS, *PURCHASE_DOCUMENTS, *PAYMENT_DOCUMENTS),
            amend=(*SALES_DOCUMENTS, *PURCHASE_DOCUMENTS, *PAYMENT_DOCUMENTS),
        ),
        *_view("accounting", *ACCOUNTING_MODELS),
        # The chart is the accountant's to shape; the two ledgers are append-only
        # and the admin registers them read-only whatever this says (CLAUDE.md §3).
        *_crud("accounting", "account", "warehouse"),
        *_view("masters", *MASTER_MODELS),
        *_view("reports", "companyprofile"),
        perms.VIEW_COST_PRICE,
        perms.VIEW_REPORTS,
        perms.VIEW_REPORTS_FINANCIAL,
        perms.OVERRIDE_CREDIT_LIMIT,
        perms.VIEW_ALL_ROUTES,
    ],
)

OPERATOR = GroupSpec(
    name="Operator",
    description=(
        "The counter. Writes sales and purchase documents and posts them. "
        "Never cancels or amends one — that is the accountant's call."
    ),
    permissions=[
        *_crud("sales", *SALES_MODELS),
        *_crud("purchasing", *PURCHASE_MODELS),
        *_lifecycle(post=(*SALES_DOCUMENTS, *PURCHASE_DOCUMENTS)),
        *_view("payments", *PAYMENT_MODELS),
        *_view("masters", *MASTER_MODELS),
        *_view("accounting", *ACCOUNTING_MODELS),
        perms.VIEW_REPORTS,
        perms.VIEW_ALL_ROUTES,
        # An Operator enters supplier bills, and a supplier bill *is* a list of
        # cost prices — the rate is on the paper in front of them and they have
        # to be able to check what they typed. So they hold this, and what they
        # do not hold is the ability to reverse anything or to open the
        # financial statements.
        #
        # If this installation would rather an Operator could not see cost at
        # all, take this line out: they then also lose the purchase entry
        # screens, because a purchase grid with the rate column masked is a
        # screen nobody can use.
        perms.VIEW_COST_PRICE,
    ],
)

BOOKER = GroupSpec(
    name="Booker",
    description=(
        "The round. Sees the shops on their own routes, takes money from them, "
        "and prints the day sheet. No cost prices."
    ),
    permissions=[
        *_view("masters", "client", "route", "seller", "item"),
        *_view("sales", "salesinvoice", "salesinvoiceline"),
        *_view("payments", *PAYMENT_MODELS),
        # Taking money is the job. Allocating it to the bills it settles is the
        # same act from the shop's point of view, so it comes with it.
        "payments.add_payment",
        "payments.change_payment",
        "payments.add_paymentallocation",
        *_lifecycle(post=(("payments", "payment"),)),
        perms.VIEW_REPORTS,
        # Deliberately **not** VIEW_ALL_ROUTES. That absence is what puts this
        # group through apps.accounts.scoping and limits it to its own routes.
        # Deliberately not VIEW_COST_PRICE: a booker standing in a shop must not
        # be able to read out the margin.
    ],
)

VIEWER = GroupSpec(
    name="Viewer",
    description="Reads. No cost prices, no financial statements, no writes at all.",
    permissions=[
        *_view("sales", *SALES_MODELS),
        *_view("purchasing", *PURCHASE_MODELS),
        *_view("payments", *PAYMENT_MODELS),
        *_view("masters", *MASTER_MODELS),
        *_view("accounting", *ACCOUNTING_MODELS),
        *_view("reports", "companyprofile"),
        perms.VIEW_REPORTS,
        perms.VIEW_ALL_ROUTES,
    ],
)

#: In the order they appear in the admin, which is roughly most access first.
GROUP_DEFINITIONS: tuple[GroupSpec, ...] = (ADMIN, ACCOUNTANT, OPERATOR, BOOKER, VIEWER)

#: The names, for the tests and for the user admin's help text.
GROUP_NAMES: tuple[str, ...] = tuple(spec.name for spec in GROUP_DEFINITIONS)


def group_spec(name: str) -> GroupSpec:
    for spec in GROUP_DEFINITIONS:
        if spec.name == name:
            return spec
    raise KeyError(f"No group named {name!r}; expected one of {', '.join(GROUP_NAMES)}.")


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------
def seed_groups(group_model, permission_model) -> dict[str, tuple[int, int]]:
    """Create the five groups and give them their permissions.

    ``group_model`` and ``permission_model`` are passed in rather than imported
    so the data migration can hand over its historical models — a migration that
    imports the live model breaks the day the model gains a field. Same
    reasoning as :func:`apps.accounting.chart.seed_chart_of_accounts`.

    Returns ``{group name: (added, already there)}``.

    Additive: a permission is added to a group that lacks it, and none is ever
    removed. See the module docstring for why.
    """
    by_key = {
        (app_label, codename): pk
        for pk, app_label, codename in permission_model.objects.values_list(
            "pk", "content_type__app_label", "codename"
        )
    }
    every_pk = set(by_key.values())

    result: dict[str, tuple[int, int]] = {}
    for spec in GROUP_DEFINITIONS:
        group, _created = group_model.objects.get_or_create(name=spec.name)

        if spec.everything:
            wanted = every_pk
        else:
            wanted = set()
            for permission in dict.fromkeys(spec.permissions):
                key = perms.split(permission)
                if key not in by_key:
                    raise LookupError(
                        f"Group {spec.name!r} wants {permission!r}, which does not exist. "
                        f"Declare it in the model's Meta.permissions and run makemigrations."
                    )
                wanted.add(by_key[key])

        held = set(group.permissions.values_list("pk", flat=True))
        missing = wanted - held
        if missing:
            group.permissions.add(*missing)
        result[spec.name] = (len(missing), len(wanted & held))

    return result


__all__ = [
    "ACCOUNTANT",
    "ADMIN",
    "BOOKER",
    "GROUP_DEFINITIONS",
    "GROUP_NAMES",
    "OPERATOR",
    "VIEWER",
    "GroupSpec",
    "group_spec",
    "seed_groups",
]
