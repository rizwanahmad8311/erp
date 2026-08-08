from django.apps import AppConfig


class AccountsConfig(AppConfig):
    """Who may do what: groups, permissions, and the routes a booker can see.

    Django's own ``Group`` and model permissions are the entire mechanism. There
    is no RBAC package here and there must not be one — "module-level access" is
    a permission on a model and a group holding it, which Django already does,
    and a second layer on top would be a second answer to the same question.
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.accounts"
    label = "accounts"
    verbose_name = "Users & access"
