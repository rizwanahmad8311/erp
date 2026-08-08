from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import path

urlpatterns = [
    path("admin/", admin.site.urls),
]

# App URLConfs get mounted here as each app grows a UI:
#   path("masters/", include("apps.masters.urls")),
#   path("sales/", include("apps.sales.urls")),

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
