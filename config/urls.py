from django.conf import settings
from django.contrib import admin
from django.urls import include, path, re_path
from django.views.static import serve

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("apps.core.urls")),
    path("me/", include("apps.accounts.urls")),
    path("purchasing/", include("apps.purchasing.urls")),
    path("sales/", include("apps.sales.urls")),
    path("payments/", include("apps.payments.urls")),
    path("reports/", include("apps.reports.urls")),
]

# App URLConfs get mounted here as each app grows a UI:
#   path("masters/", include("apps.masters.urls")),

# Media — the company logo, and nothing else today.
#
# Served by Django itself, in production as well as in development, which is
# normally the wrong answer and is the right one here for the same reason
# WhiteNoise serves the static files (CLAUDE.md §8): there is no nginx on the
# office PC, there is no reverse proxy to put one behind, and waitress serves
# the whole site from a single process on a LAN. The alternative is a broken
# image at the top of every printed invoice.
#
# WhiteNoise deliberately does not do this: it serves STATIC_ROOT, which is
# built by collectstatic from files committed to git. The logo is uploaded by
# the operator and lives under MEDIA_ROOT, which is git-ignored.
urlpatterns += [
    re_path(r"^media/(?P<path>.*)$", serve, {"document_root": settings.MEDIA_ROOT}),
]
