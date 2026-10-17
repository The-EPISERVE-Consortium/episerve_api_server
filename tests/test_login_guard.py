"""The lock against password guessing: counting, the window, the three keys and the login endpoint."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import login_guard
from app.routers import auth as auth_router
from tests.fakes import install_users, make_user

ADA = make_user(1, ("runs",), "right password", "ada@example.org")
BOB = make_user(2, (), "bobs password", "bob@example.org")


@pytest.fixture(autouse=True)
def _people_and_clock(monkeypatch):
    install_users(monkeypatch, ADA, BOB)
    clock = {"now": 1000.0}
    monkeypatch.setattr(login_guard, "_clock", lambda: clock["now"])
    return clock


def _fail(email="ada@example.org", ip="1.1.1.1", times=1):
    for _ in range(times):
        assert login_guard.authenticate(email, "wrong", ip) is None


def test_five_failures_from_one_ip_lock_every_login_from_it_even_the_right_password(_people_and_clock):
    _fail(times=3)
    _fail(email="bob@example.org", times=2)  # the failures count together, whatever account they were for
    with pytest.raises(login_guard.Locked) as locked:
        login_guard.authenticate("ada@example.org", "right password", "1.1.1.1")
    assert locked.value.retry_after == 900
    with pytest.raises(login_guard.Locked):
        login_guard.authenticate("bob@example.org", "bobs password", "1.1.1.1")
    # another machine is not affected
    assert login_guard.authenticate("ada@example.org", "right password", "2.2.2.2") == ADA


def test_the_lock_ends_when_the_failures_leave_the_window(_people_and_clock):
    _fail(times=5)
    _people_and_clock["now"] += 899
    with pytest.raises(login_guard.Locked) as locked:
        login_guard.authenticate("ada@example.org", "right password", "1.1.1.1")
    assert locked.value.retry_after == 1
    _people_and_clock["now"] += 1
    assert login_guard.authenticate("ada@example.org", "right password", "1.1.1.1") == ADA


def test_a_success_does_not_lower_the_count_of_the_ip(_people_and_clock):
    _fail(times=4)
    assert login_guard.authenticate("bob@example.org", "bobs password", "1.1.1.1") == BOB
    _fail()  # the fifth failure from this IP counts although a login succeeded in between
    with pytest.raises(login_guard.Locked):
        login_guard.authenticate("bob@example.org", "bobs password", "1.1.1.1")


def test_a_success_clears_the_failures_counted_for_the_email(_people_and_clock):
    for i in range(29):
        _fail(ip=f"10.0.1.{i}")
    assert login_guard.authenticate("ada@example.org", "right password", "10.0.2.1") == ADA
    for i in range(29):
        _fail(ip=f"10.0.3.{i}")  # would be locked at 30 without the reset
    assert login_guard.retry_after("ada@example.org", "10.0.4.1") == 0


def test_one_account_guessed_from_many_ips_is_locked_after_thirty(_people_and_clock):
    for i in range(30):
        _fail(ip=f"10.0.0.{i}")  # every IP stays far below its own limit
    with pytest.raises(login_guard.Locked):
        login_guard.authenticate("ada@example.org", "right password", "10.9.9.9")


def test_email_case_and_blanks_do_not_help_to_dodge_the_lock(_people_and_clock):
    for i in range(30):
        _fail(email=["ada@example.org", "  ADA@example.org ", "Ada@Example.org"][i % 3], ip=f"10.1.0.{i}")
    with pytest.raises(login_guard.Locked):
        login_guard.authenticate("ada@EXAMPLE.org", "right password", "10.1.9.9")


def test_memory_is_bounded(monkeypatch):
    monkeypatch.setattr(login_guard, "MAX_KEYS", 50)
    for i in range(100):
        _fail(email=f"x{i}@example.org", ip=f"3.3.3.{i}")
    assert len(login_guard._failures) <= 50


def test_the_ip_is_the_one_the_gateway_appended():
    assert login_guard.client_ip("6.6.6.6, 203.0.113.9", "10.0.0.1") == "203.0.113.9"
    assert login_guard.client_ip(None, "10.0.0.1") == "10.0.0.1"
    assert login_guard.client_ip("", None) == "unknown"


def test_token_endpoint_answers_429_with_retry_after_once_locked():
    app = FastAPI()
    app.include_router(auth_router.router)
    client = TestClient(app)
    body = {"username": "ada@example.org", "password": "wrong"}
    for _ in range(5):
        assert client.post("/auth/token", json=body).status_code == 401
    locked = client.post("/auth/token", json={**body, "password": "right password"})
    assert locked.status_code == 429
    assert int(locked.headers["Retry-After"]) > 0
    # a different client address is not affected
    other = client.post("/auth/token", json={**body, "password": "right password"},
                        headers={"X-Forwarded-For": "198.51.100.7"})
    assert other.status_code == 200
