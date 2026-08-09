"""The backup screen's URLs.

``<str:filename>`` rather than a primary key on the download: the archives are
files on disk and the log rows are a record *of* them, so a file that was copied
onto the machine by hand — off the USB stick, out of Google Drive — is still
downloadable. The view resolves the name inside BACKUP_ROOT and refuses anything
that escapes it.
"""

from django.urls import path

from . import views

app_name = "backup"

urlpatterns = [
    path("", views.index, name="index"),
    path("status/", views.status, name="status"),
    path("run/", views.start, name="run"),
    path("download/<str:filename>/", views.download, name="download"),
]
