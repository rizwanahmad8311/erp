"""User management: create a login, put it in a group, give a booker their routes.

Django's own ``UserAdmin`` already does most of this and is not replaced — it is
re-registered with Unfold's styling and three additions, each of which exists
because leaving it out produces a specific mistake:

**The group is the access.** The changelist shows it, filters on it and says so,
because a user in no group can sign in and do nothing, and that looks like a
broken system rather than an unfinished setup.

**A booker's seller is on the same screen as their group.** Row-level scope
comes from ``UserProfile.seller`` -> ``RouteSeller`` -> routes
(:mod:`apps.accounts.scoping`), so a Booker created without one sees **no**
shops. That is the safe failure and it is still a failure, so the form says
which routes the choice grants and the changelist shows when it is missing.

**A new login must change its password.** It is created with one an
administrator typed and therefore knows —
:class:`apps.accounts.middleware.ForcePasswordChangeMiddleware` blocks every
other page until it is changed.

Deactivating rather than deleting is the only supported way to remove somebody.
A user is referenced by ``created_by`` and ``updated_by`` on every document they
ever touched; deleting one either fails on a ``PROTECT`` or takes an audit trail
with it.
"""

from __future__ import annotations

from django.contrib import admin, messages
from django.contrib.auth.admin import GroupAdmin as DjangoGroupAdmin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.models import Group, User
from django.utils.html import format_html
from unfold.admin import ModelAdmin, StackedInline
from unfold.forms import AdminPasswordChangeForm, UserChangeForm, UserCreationForm

from apps.masters.models import RouteSeller

from .groups import GROUP_NAMES
from .models import UserProfile

# Django registers both of these itself. Unregistering first is what lets this
# app replace them with the Unfold-styled versions below rather than raising
# AlreadyRegistered at import time.
admin.site.unregister(Group)


class UserProfileInline(StackedInline):
    """The booker link and the password flag, on the user's own page.

    An inline rather than a separate screen: "which routes does Imran see" is a
    question about Imran, and making somebody visit a second model to answer it
    is how a booker ends up with no seller and an empty recovery sheet.
    """

    model = UserProfile
    # Named explicitly: TimeStampedModel carries created_by and updated_by, so
    # there are three foreign keys to User on this model and Django cannot pick.
    fk_name = "user"
    can_delete = False
    verbose_name_plural = "Access"
    fields = ("seller", "routes_display", "must_change_password", "password_changed_at")
    readonly_fields = ("routes_display", "password_changed_at")
    autocomplete_fields = ("seller",)
    extra = 0

    @admin.display(description="Routes this login can see")
    def routes_display(self, obj) -> str:
        """Spelled out, because the seller field alone does not say it.

        The chain is seller -> RouteSeller -> route, and an administrator
        picking a name from a dropdown has no way to know which beats it grants
        unless the screen tells them.
        """
        if obj is None or obj.pk is None or obj.seller_id is None:
            return "Every route — this login is not limited to a seller's beats."
        routes = (
            RouteSeller.objects.filter(seller_id=obj.seller_id)
            .select_related("route")
            .order_by("route__code")
        )
        if not routes:
            return format_html(
                '<span style="color:#bd413f">{}</span>',
                "This seller is on no route, so this login sees no shops at all. "
                "Assign them a route first.",
            )
        return ", ".join(f"{link.route.code} — {link.route.name}" for link in routes)


admin.site.unregister(User)


@admin.register(User)
class UserAdmin(DjangoUserAdmin, ModelAdmin):
    """Django's user admin, styled by Unfold, with access on the same page.

    Order matters: ``DjangoUserAdmin`` first so its password handling, its
    ``add_fieldsets`` and its ``user_change_password`` view all still work, and
    ``ModelAdmin`` second so every Unfold template and widget applies. The other
    way round gives an Unfold page whose password field is a raw hash — which
    looks fine, and is the bug.
    """

    form = UserChangeForm
    add_form = UserCreationForm
    change_password_form = AdminPasswordChangeForm
    inlines = (UserProfileInline,)

    list_display = ("username", "full_name", "group_list", "seller_display", "is_active", "flags")
    list_filter = ("is_active", "is_staff", "groups")
    search_fields = ("username", "first_name", "last_name", "email")
    ordering = ("username",)
    filter_horizontal = ("groups", "user_permissions")

    fieldsets = (
        (None, {"fields": ("username", "password")}),
        ("Person", {"fields": ("first_name", "last_name", "email")}),
        (
            "Access",
            {
                "fields": ("is_active", "is_staff", "groups"),
                "description": (
                    "Access is the <strong>group</strong>. The five are "
                    f"{', '.join(GROUP_NAMES)} — see apps/accounts/groups.py for exactly "
                    "what each one may do. Per-user permissions below are for the rare "
                    "exception; if a role needs something, change the group instead, "
                    "because the next person hired into it will need it too."
                ),
            },
        ),
        (
            "Exceptions",
            {"fields": ("user_permissions", "is_superuser"), "classes": ["collapse"]},
        ),
        ("Audit", {"fields": ("last_login", "date_joined"), "classes": ["collapse"]}),
    )

    add_fieldsets = (
        (
            None,
            {
                "classes": ["wide"],
                "fields": ("username", "password1", "password2"),
                "description": (
                    "The new login must change this password before it can do anything "
                    "else. Put them in a group on the next screen — a user in no group "
                    "can sign in and do nothing at all."
                ),
            },
        ),
    )

    def get_inline_instances(self, request, obj=None):
        """No inlines on the add screen.

        Django's add step asks for a username and a password and nothing else,
        which is deliberate — the profile has no user to hang off until the row
        exists. Leaving the inline on would put a seller dropdown on a form that
        cannot save one, and the missing management form makes the whole
        creation fail validation with nothing on screen to explain it.
        """
        if obj is None:
            return []
        return super().get_inline_instances(request, obj)

    # ------------------------------------------------------------------
    # Columns
    # ------------------------------------------------------------------
    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related("profile", "profile__seller")
            .prefetch_related("groups")
        )

    @admin.display(description="Name")
    def full_name(self, obj) -> str:
        return obj.get_full_name() or "—"

    @admin.display(description="Group")
    def group_list(self, obj) -> str:
        """What this user may do, in one column.

        Named rather than counted, and loud when empty: a user in no group is
        the most common setup mistake and it presents as "the system is broken".
        """
        names = [group.name for group in obj.groups.all()]
        if not names:
            return format_html('<span style="color:#bd413f">{}</span>', "No group — can do nothing")
        return ", ".join(names)

    @admin.display(description="Booker for")
    def seller_display(self, obj) -> str:
        profile = getattr(obj, "profile", None)
        if profile is None or profile.seller_id is None:
            return "—"
        return str(profile.seller)

    @admin.display(description="")
    def flags(self, obj) -> str:
        profile = getattr(obj, "profile", None)
        if profile is not None and profile.must_change_password:
            return format_html('<span style="color:#b18827">{}</span>', "Password not set yet")
        return ""

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    actions = ("deactivate", "require_password_change")

    @admin.action(description="Deactivate the selected logins")
    def deactivate(self, request, queryset):
        """Switch a login off. **Never delete one.**

        A user is named by ``created_by`` and ``updated_by`` on every document
        they ever touched. Deleting one either refuses on a foreign key or takes
        part of the audit trail with it, and neither is what somebody clicking
        "remove this person" wants — they want the login to stop working, which
        is this.
        """
        changed = queryset.exclude(pk=request.user.pk).update(is_active=False)
        skipped = queryset.filter(pk=request.user.pk).count()
        self.message_user(
            request,
            f"{changed} login{'s' if changed != 1 else ''} deactivated."
            + (" Your own was left alone." if skipped else ""),
            messages.SUCCESS,
        )

    @admin.action(description="Require a new password at next sign-in")
    def require_password_change(self, request, queryset):
        for user in queryset:
            UserProfile.for_user(user).require_password_change(user)
        self.message_user(
            request,
            f"{queryset.count()} login(s) must set a new password before doing anything else.",
            messages.SUCCESS,
        )

    # ------------------------------------------------------------------
    def save_model(self, request, obj, form, change):
        """Create the login, then demand it be changed.

        Done here rather than in a signal for the reason this codebase avoids
        signals generally: a hook that runs invisibly at the call site is one
        nobody can find when it does not fire.
        """
        super().save_model(request, obj, form, change)
        profile = UserProfile.for_user(obj)
        if not change:
            profile.require_password_change(obj)
        elif "password" in form.changed_data:
            # An administrator has just reset somebody's password, so they know
            # it again — the same situation as a new login.
            profile.require_password_change(obj)


@admin.register(Group)
class GroupAdmin(DjangoGroupAdmin, ModelAdmin):
    """The five roles, and what each one holds.

    Editable, deliberately: an installation will want a permission moved sooner
    or later. What that costs is that the seeded definition in
    ``apps/accounts/groups.py`` and this row can then disagree — which is why
    the seed is additive and never takes a permission away (see that module),
    and why a change made here survives the next migration.
    """

    list_display = ("name", "permission_count", "member_count")
    search_fields = ("name",)
    filter_horizontal = ("permissions",)

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("permissions", "user_set")

    @admin.display(description="Permissions")
    def permission_count(self, obj) -> int:
        return obj.permissions.count()

    @admin.display(description="People")
    def member_count(self, obj) -> int:
        return obj.user_set.count()


@admin.register(UserProfile)
class UserProfileAdmin(ModelAdmin):
    """The profiles on their own, for looking up "who covers this route".

    Editing happens on the user's page, where the group is; this exists to be
    searched and filtered from the other direction.
    """

    list_display = ("user", "seller", "must_change_password", "password_changed_at")
    list_filter = ("must_change_password", "seller")
    search_fields = ("user__username", "seller__code", "seller__name")
    autocomplete_fields = ("user", "seller")
    readonly_fields = ("password_changed_at",)

    def has_add_permission(self, request):
        """No. A profile belongs to a user and is created with one."""
        return False
