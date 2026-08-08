"""Who a login *is*, beyond a username and a password.

Django's ``User`` answers "may this person do X" through groups and permissions,
and that is the whole authorisation mechanism here (see
:mod:`apps.accounts.permissions`). Two things it cannot answer, and this model
does:

**Which routes are theirs.** A booker sees the shops on the routes they walk and
no others. The link is deliberately *through* :class:`~apps.masters.models.Seller`
rather than straight to :class:`~apps.masters.models.Route`: a seller already has
routes through ``RouteSeller``, that is the table the office maintains when a
beat is reassigned, and a second list of routes on the user would be a second
answer that drifts the first time somebody updates one and not the other.

**Whether they still have the password somebody else typed for them.** A user
created by an administrator is created with a password that administrator knows,
so the first thing that login must do is change it —
:class:`apps.accounts.middleware.ForcePasswordChangeMiddleware`.

A profile is created on demand by :meth:`UserProfile.for_user`, not by a signal.
Signals are invisible at the call site, and this codebase does not use them
(CLAUDE.md §4 says so for financial writes; the same reasoning applies here, at
much lower stakes). Every read goes through the accessor, so a user created in a
shell or a fixture cannot end up without one.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models

from apps.core.models import TimeStampedModel


class UserProfile(TimeStampedModel):
    """The business facts attached to a login.

    Deliberately **not** a custom `AUTH_USER_MODEL`. Swapping the user model is
    a one-way decision that has to be made before the first migration, and this
    project made the other one — a profile hanging off ``auth.User`` costs one
    join and can be added to an installation that already has users in it.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="profile",
    )
    seller = models.ForeignKey(
        "masters.Seller",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="users",
        help_text=(
            "The booker this login belongs to. Their routes come from RouteSeller, "
            "and a user without 'view all routes' sees only those routes' shops."
        ),
    )
    must_change_password = models.BooleanField(
        # False, deliberately. The flag means "somebody else chose this
        # password", and only :meth:`require_password_change` — called by the
        # user admin when it creates a login or resets one — knows that. A
        # default of True would lock every account that predates this app,
        # including the superuser who ran `createsuperuser` and typed their own
        # password, into a change loop on the first upgrade.
        default=False,
        help_text=(
            "Set when an administrator creates or resets this login. Until it is "
            "cleared, every page redirects to the password change screen."
        ),
    )
    password_hash_at_grant = models.CharField(
        max_length=128,
        blank=True,
        default="",
        editable=False,
        help_text=(
            "The hash of the password the administrator set. Compared against the "
            "user's current one to notice that they have changed it."
        ),
    )
    password_changed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When this user last set their own password. Never typed in.",
    )

    class Meta:
        verbose_name = "user profile"
        verbose_name_plural = "user profiles"
        permissions = [
            (
                "view_all_routes",
                "Can see clients on every route, not only their own seller's",
            ),
            (
                "manage_users",
                "Can create users, assign groups and routes, and deactivate logins",
            ),
        ]

    def __str__(self) -> str:
        return f"Profile for {self.user}"

    # ------------------------------------------------------------------
    @classmethod
    def for_user(cls, user) -> UserProfile:
        """This user's profile, created empty on first use.

        Never raises and never returns ``None``. A user created before this app
        existed, or from a shell, or by a test fixture, gets one the first time
        anything asks — which is what makes it safe for the scoping layer to
        assume every user has a profile.
        """
        profile, _created = cls.objects.get_or_create(user=user)
        return profile

    @property
    def route_ids(self) -> list[int]:
        """The routes this login's seller is assigned to, through ``RouteSeller``.

        Empty when no seller is linked — and that emptiness is load-bearing: a
        scoped user with no seller sees **nothing**, not everything. See
        :func:`apps.accounts.scoping.visible_route_ids`.
        """
        if self.seller_id is None:
            return []
        from apps.masters.models import RouteSeller

        return list(
            RouteSeller.objects.filter(seller_id=self.seller_id)
            .order_by("route_id")
            .values_list("route_id", flat=True)
            .distinct()
        )

    # ------------------------------------------------------------------
    # The first password
    # ------------------------------------------------------------------
    def require_password_change(self, user=None) -> None:
        """Demand a new password, and remember the one being replaced.

        Called by the user admin after it creates a login or resets a password.
        Storing the hash is what makes :meth:`resolve_password_change` able to
        notice the change **however it was made** — the admin's own screen, a
        management command, a shell session — without a signal, a view override
        or a second place that has to remember to clear the flag.
        """
        user = user or self.user
        self.must_change_password = True
        self.password_hash_at_grant = user.password or ""
        self.save(update_fields=["must_change_password", "password_hash_at_grant", "updated_at"])

    def resolve_password_change(self, user) -> bool:
        """Whether a password change is still outstanding, clearing it if not.

        Compares the user's current password hash against the one an
        administrator set. A different hash means they have changed it, so the
        flag comes off here rather than in whatever screen happened to do it —
        which is why changing a password from the shell also satisfies this.

        Returns ``True`` while the person is still using the password somebody
        else chose for them.
        """
        if not self.must_change_password:
            return False
        if self.password_hash_at_grant and user.password != self.password_hash_at_grant:
            self.mark_password_changed()
            return False
        return True

    def mark_password_changed(self) -> None:
        """Clear the forced-change flag and stamp when."""
        from django.utils import timezone

        self.must_change_password = False
        self.password_hash_at_grant = ""
        self.password_changed_at = timezone.now()
        self.save(
            update_fields=[
                "must_change_password",
                "password_hash_at_grant",
                "password_changed_at",
                "updated_at",
            ]
        )


__all__ = ["UserProfile"]
