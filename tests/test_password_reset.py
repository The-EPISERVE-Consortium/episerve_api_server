"""The forgotten-password flow: the mailed token and the new password (no database, no mail)."""

import pytest

from app import password_reset, users
from tests.fakes import install_users, make_user

ADA = make_user(1, ("runs",), "old password", "ada@example.org")


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    install_users(monkeypatch, ADA)
    password_reset._last_request.clear()
    sent = []
    monkeypatch.setattr(password_reset.mailer, "send_mail", lambda to, subject, text: sent.append((to, text)) or True)
    return sent


def test_reset_mail_goes_only_to_known_users_and_at_most_once_a_minute(_setup):
    password_reset.request_reset("nobody@example.org")
    assert _setup == []
    password_reset.request_reset("Ada@Example.org")
    password_reset.request_reset("ada@example.org")
    assert len(_setup) == 1
    to, text = _setup[0]
    assert to == "ada@example.org"
    token = text.split("token=")[1].split()[0]
    assert users.verify_reset_token(token) == ADA


def test_the_link_sets_a_new_password_once(monkeypatch):
    stored = {}
    monkeypatch.setattr(users, "set_password", lambda email, pw: stored.update({email: pw}) or True)
    token = users.issue_reset_token(ADA)
    assert password_reset.complete_reset(token, "short") is not None
    assert password_reset.complete_reset("garbage", "a long new password") is not None
    assert password_reset.complete_reset(token, "a long new password") is None
    assert stored == {"ada@example.org": "a long new password"}
    install_users(monkeypatch, make_user(1, ("runs",), "a long new password", "ada@example.org"))
    assert users.verify_reset_token(token) is None  # used up: the password hash changed


def test_reset_tokens_expire_and_are_not_login_tokens(monkeypatch):
    token = users.issue_reset_token(ADA)
    monkeypatch.setattr(users, "RESET_VALID_SECONDS", -1)
    assert users.verify_reset_token(users.issue_reset_token(ADA)) is None
    assert users.verify_token(token) is None
    assert users.verify_reset_token(users.issue_token(ADA)) is None
