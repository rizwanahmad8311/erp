"""Every permission this system checks, named once.

Django's own `Group` and model permissions are the whole mechanism — there is no
RBAC package here and there must not be one. What "module-level access" means in
Django is a permission on a model and a `Group` holding it, and a third-party
layer on top would be a second answer to "may this person open the purchase
screen" that eventually disagrees with the first.

What this module adds is **names**. A permission is a string, and a string typed
into a view, a template, a group definition and a test is a string that will be
misspelled in exactly one of those four — where it fails open, because
``user.has_perm("sales.add_salesinvoce")`` is ``False`` for everybody and a view
that nobody can reach looks like a permissions problem rather than a typo. So
every permission is a constant here, the group definitions are built from these
constants, and :func:`assert_permissions_exist` fails the build if one of them
does not exist in the database after a migration.

Three kinds of permission live here:

**Model permissions** — ``add`` / ``change`` / ``delete`` / ``view``, created by
Django for every model. These are the module gates: ``sales.view_salesinvoice``
is what decides whether the Sales entry in the sidebar is drawn.

**Lifecycle permissions** — ``post_`` / ``cancel_`` / ``amend_`` per document
type, declared in each model's ``Meta.permissions`` and derived rather than
typed by :class:`~apps.core.models.DocumentModel`. They are separate from
``change_`` on purpose: posting is not editing, and an Operator who may write a
bill all day must not be able to reverse one.

**Capability permissions** — the handful that are not about a model at all:
overriding a credit limit, seeing a cost price, running a backup, opening the
financial statements. Each is hung on the model it is *about*, because Django
has nowhere else to put a permission.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------
#: Post a sales invoice that takes a shop past its credit limit. Checked by
#: :func:`apps.sales.services.may_override_credit_limit`.
OVERRIDE_CREDIT_LIMIT = "sales.override_credit_limit"

#: Issue stock a warehouse does not hold. A negative position has no cost behind
#: it, so every later issue out of it is valued at a guess — see
#: ``settings.ALLOW_NEGATIVE_STOCK``. The permission is the per-person half of
#: that switch.
OVERRIDE_NEGATIVE_STOCK = "accounting.override_negative_stock"

#: See what goods cost: COGS, margin, valuation rate, purchase rates. The one
#: permission that changes what a page *contains* rather than whether it opens,
#: which is why it has to be honoured in the CSV and the PDF as well as on the
#: screen — see :mod:`apps.reports.framework`.
VIEW_COST_PRICE = "masters.view_cost_price"

#: Take a copy of the database.
RUN_BACKUP = "backup.run_backup"

#: Overwrite the database from a copy. Deliberately separate from
#: :data:`RUN_BACKUP`: taking a backup is safe and routine, and restoring one
#: destroys everything posted since it was taken.
RESTORE_BACKUP = "backup.restore_backup"

#: Open the reports section at all.
VIEW_REPORTS = "reports.view_reports"

#: Open the statements that show what the business is worth and what it earned —
#: Profit & Loss, Balance Sheet, Trial Balance. Separate from
#: :data:`VIEW_REPORTS` because a stock balance is an operational question and
#: the owner's profit is not.
VIEW_REPORTS_FINANCIAL = "reports.view_reports_financial"

#: See every route's shops. Without it a user is scoped to the routes their
#: seller is assigned to through ``RouteSeller`` — see :mod:`apps.accounts.scoping`.
VIEW_ALL_ROUTES = "accounts.view_all_routes"

#: Create users, put them in groups, assign their routes, deactivate them.
MANAGE_USERS = "accounts.manage_users"


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------
#: The document types that have a full lifecycle, as ``app_label.model_name``.
#: Every one of them gets ``post_`` / ``cancel_`` / ``amend_``, declared in its
#: own ``Meta.permissions`` and derived by
#: :meth:`apps.core.models.DocumentModel.post_permission` and friends.
DOCUMENT_TYPES = (
    ("sales", "salesinvoice"),
    ("sales", "salesreturn"),
    ("purchasing", "purchaseinvoice"),
    ("purchasing", "purchasereturn"),
    ("payments", "payment"),
    ("payments", "chequeevent"),
)


def lifecycle_permissions(app_label: str, model_name: str) -> tuple[str, str, str]:
    """``("sales.post_salesinvoice", "sales.cancel_…", "sales.amend_…")``."""
    return (
        f"{app_label}.post_{model_name}",
        f"{app_label}.cancel_{model_name}",
        f"{app_label}.amend_{model_name}",
    )


def model_permissions(app_label: str, model_name: str, *actions: str) -> tuple[str, ...]:
    """``model_permissions("sales", "salesinvoice", "view", "add")``."""
    return tuple(f"{app_label}.{action}_{model_name}" for action in actions)


def every_permission() -> list[str]:
    """Every permission named in this module, for the Admin group and the tests.

    Derived from the module's own constants rather than listed a second time, so
    a capability added above is one the Admin group holds without anybody
    remembering to add it.
    """
    named = [
        value
        for name, value in sorted(globals().items())
        if name.isupper() and isinstance(value, str) and "." in value
    ]
    lifecycle = [
        permission
        for app_label, model_name in DOCUMENT_TYPES
        for permission in lifecycle_permissions(app_label, model_name)
    ]
    return sorted(set(named + lifecycle))


def split(permission: str) -> tuple[str, str]:
    """``"sales.view_salesinvoice"`` -> ``("sales", "view_salesinvoice")``."""
    app_label, _, codename = permission.partition(".")
    if not app_label or not codename:
        raise ValueError(f"{permission!r} is not a permission; expected 'app_label.codename'.")
    return app_label, codename


def assert_permissions_exist(permission_model, permissions) -> None:
    """Raise unless every named permission is really in the database.

    Called by the group-seeding migration and by ``tests/test_permissions.py``.
    A misspelled permission fails **open** — nobody holds it, so a view guarded
    by it is unreachable and looks like a policy decision rather than a typo —
    so it has to be caught somewhere, and this is that somewhere.
    """
    wanted = {split(permission) for permission in permissions}
    found = {
        (app_label, codename)
        for app_label, codename in permission_model.objects.values_list(
            "content_type__app_label", "codename"
        )
    }
    missing = sorted(f"{app}.{codename}" for app, codename in wanted - found)
    if missing:
        raise LookupError(
            f"These permissions are named in apps.accounts.permissions but do not exist: "
            f"{', '.join(missing)}. Declare each in its model's Meta.permissions and run "
            f"makemigrations."
        )


__all__ = [
    "DOCUMENT_TYPES",
    "MANAGE_USERS",
    "OVERRIDE_CREDIT_LIMIT",
    "OVERRIDE_NEGATIVE_STOCK",
    "RESTORE_BACKUP",
    "RUN_BACKUP",
    "VIEW_ALL_ROUTES",
    "VIEW_COST_PRICE",
    "VIEW_REPORTS",
    "VIEW_REPORTS_FINANCIAL",
    "assert_permissions_exist",
    "every_permission",
    "lifecycle_permissions",
    "model_permissions",
    "split",
]
