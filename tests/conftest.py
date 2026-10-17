"""Test configuration.

Allow insecure default secrets so importing ``app.main`` (which now fails
closed on a shipped-default SESSION_SECRET) works in the test suite.
"""

import os

os.environ.setdefault("ALLOW_INSECURE_DEFAULT_SECRETS", "1")

import pytest

from tests.fakes import install_users, make_user

ADMIN = make_user()


@pytest.fixture(autouse=True)
def _known_users(monkeypatch):
    """Without a database: user 1 is an admin (the tests' AUTH headers belong to it)."""
    install_users(monkeypatch, ADMIN)


@pytest.fixture(autouse=True)
def _fresh_login_guard():
    from app import login_guard
    login_guard._failures.clear()
    yield
    login_guard._failures.clear()
