"""No bare traceback ever reaches an operator.

Two halves, and both matter:

* the **page** says what happened, whether anything was saved, and what to do;
* the **traceback** is written to the log with the same reference the page shows,
  so a phone call about "reference PQ7K2MBX" becomes a log search rather than an
  archaeology exercise.

Business refusals are not tested here — those are `CoreError`, every view already
catches them, and they arrive beside the field. This is the other kind: a bug.
"""

from __future__ import annotations

import logging

import pytest
from django.urls import path

from apps.core.errors import REFERENCE_LENGTH, new_reference

pytestmark = pytest.mark.django_db


def _boom(request):
    raise RuntimeError("a bug nobody expected")


urlpatterns = [path("boom/", _boom)]

handler400 = "apps.core.errors.bad_request"
handler403 = "apps.core.errors.permission_denied"
handler404 = "apps.core.errors.not_found"
handler500 = "apps.core.errors.server_error"


class TestTheReference:
    def test_it_is_readable_over_a_telephone(self):
        """No vowels, no 0/O, no 1/I/L — it gets read aloud to support."""
        for _ in range(200):
            reference = new_reference()
            assert len(reference) == REFERENCE_LENGTH
            assert not set(reference) & set("AEIOU01ILO")

    def test_two_references_differ(self):
        assert len({new_reference() for _ in range(500)}) > 490


@pytest.mark.urls("tests.test_error_pages")
class TestTheFiveHundredPage:
    def test_it_does_not_show_a_traceback(self, client, settings, caplog):
        settings.DEBUG = False
        client.raise_request_exception = False

        with caplog.at_level(logging.ERROR):
            response = client.get("/boom/")

        body = response.content.decode()
        assert response.status_code == 500
        assert "Traceback" not in body
        assert "RuntimeError" not in body
        assert "_boom" not in body

    def test_it_says_what_happened_and_what_to_do(self, client, settings):
        settings.DEBUG = False
        client.raise_request_exception = False

        body = client.get("/boom/").content.decode()

        assert "Something went wrong" in body
        # The single most important sentence on the page: an operator who does
        # not know whether the posting landed will either re-enter it (double)
        # or not (missing).
        assert "Nothing was saved" in body
        assert "logs" in body

    def test_it_shows_a_reference(self, client, settings):
        settings.DEBUG = False
        client.raise_request_exception = False

        body = client.get("/boom/").content.decode()

        assert "Quote this when you report it" in body

    def test_the_traceback_reaches_the_log_with_the_same_reference(self, client, settings, caplog):
        """The half a developer needs. Useless if it cannot be tied to the call."""
        settings.DEBUG = False
        client.raise_request_exception = False

        with caplog.at_level(logging.ERROR, logger="apps.core.errors"):
            body = client.get("/boom/").content.decode()

        records = [r for r in caplog.records if r.name == "apps.core.errors"]
        assert records, "the unhandled error was not logged at all"

        record = records[0]
        assert record.exc_info is not None, "the traceback was not attached to the log record"
        assert "RuntimeError" in logging.Formatter().formatException(record.exc_info)

        reference = record.getMessage().split("[", 1)[1].split("]", 1)[0]
        assert reference in body, (
            "the reference on the page and the one in the log must be the same, "
            "or quoting it achieves nothing"
        )

    def test_it_logs_the_path_and_the_user(self, client, settings, caplog, django_user_model):
        settings.DEBUG = False
        client.raise_request_exception = False
        user = django_user_model.objects.create_user(username="bookkeeper", password="x")
        client.force_login(user)

        with caplog.at_level(logging.ERROR, logger="apps.core.errors"):
            client.get("/boom/")

        message = caplog.records[0].getMessage()
        assert "/boom/" in message
        assert "bookkeeper" in message


class TestTheOtherPages:
    def test_the_404_suggests_searching_rather_than_bookmarking(self, client, settings):
        settings.DEBUG = False
        body = client.get("/no-such-page-anywhere/").content.decode()

        assert "That page is not here" in body
        assert "Traceback" not in body

    def test_the_403_names_the_permission(self, client, django_user_model, settings):
        """A 403 that does not say which permission is a 403 nobody can resolve."""
        settings.DEBUG = False
        user = django_user_model.objects.create_user(username="nobody", password="x")
        client.force_login(user)

        response = client.get("/backup/")

        assert response.status_code == 403
        body = response.content.decode()
        assert "may not do that" in body
        assert "backup.run_backup" in body, "the refusal must name the permission to grant"


class TestNoScreenLeaksATraceback:
    """A smoke pass over the real screens with a signed-in user.

    Not exhaustive, and not meant to be: it is the guard that catches a template
    that raises only when rendered with real data.
    """

    @pytest.mark.parametrize(
        "url",
        ["/", "/sales/invoices/", "/payments/", "/payments/recovery/", "/reports/", "/shortcuts/"],
    )
    def test_it_renders_without_an_error_page(self, client, django_user_model, settings, url):
        settings.DEBUG = False
        user = django_user_model.objects.create_superuser(
            username="admin", password="x", email="a@example.test"
        )
        from apps.accounts.models import UserProfile

        profile = UserProfile.for_user(user)
        profile.must_change_password = False
        profile.save()
        client.force_login(user)

        response = client.get(url)

        assert response.status_code == 200, f"{url} returned {response.status_code}"
        body = response.content.decode()
        assert "Something went wrong" not in body
        assert "Traceback" not in body
