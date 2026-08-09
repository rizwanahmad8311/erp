"""Per-user settings that every screen reads."""

from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path("density/", views.set_density, name="set-density"),
]
