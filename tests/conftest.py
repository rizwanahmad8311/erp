"""Shared pytest fixtures.

model-bakery is the factory of choice; add app-specific recipes next to each
app rather than growing a global factory module here.
"""

import pytest
from django.contrib.auth import get_user_model


@pytest.fixture
def user(db):
    return get_user_model().objects.create_user(
        username="operator",
        password="operator-pass",
        email="operator@example.test",
    )


@pytest.fixture
def admin_client_logged_in(client, django_user_model, db):
    admin = django_user_model.objects.create_superuser(
        username="admin", password="admin-pass", email="admin@example.test"
    )
    client.force_login(admin)
    return client
