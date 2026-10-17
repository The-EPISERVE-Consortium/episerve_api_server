"""Passwords, personal tokens, rights and the login endpoints (no database)."""

import json

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app import users
from app.auth import optional_user, require_right
from app.routers import auth as auth_router
from tests.fakes import bearer, install_users, make_user

ADA = make_user(1, ("benchmarks",), "secret", "ada@example.org")
BOB = make_user(2, (), "hunter2", "bob@example.org")
ROOT = make_user(3, ("admin",), "pw", "root@example.org")

app = FastAPI()
app.include_router(auth_router.router)


@app.get("/benchmarks-only", dependencies=[Depends(require_right("benchmarks"))])
def benchmarks_only():
    return {}


@app.get("/who")
def who(user=Depends(optional_user)):
    return {"email": user.email if user else None}


client = TestClient(app)


@pytest.fixture(autouse=True)
def _people(monkeypatch):
    install_users(monkeypatch, ADA, BOB, ROOT)


def test_password_hash_verifies_only_the_right_password():
    stored = users.hash_password("secret")
    assert users.verify_password("secret", stored)
    assert not users.verify_password("Secret", stored)
    assert not users.verify_password("secret", "garbage")
    assert users.hash_password("secret") != stored  # salted


def test_token_belongs_to_one_user_and_one_password(monkeypatch):
    token = users.issue_token(ADA)
    assert users.verify_token(token) == ADA
    other_day = token.replace(token.split(".")[1], "20200101")
    forged = f"{BOB.id}.{token.split('.', 1)[1]}"
    changed = make_user(1, ("benchmarks",), "new password", "ada@example.org")
    for bad in (None, "", "x", other_day, forged, token + "0"):
        assert users.verify_token(bad) is None
    install_users(monkeypatch, changed)  # the password changed -> the old token is dead
    assert users.verify_token(token) is None
    install_users(monkeypatch)  # disabled or deleted users are not found
    assert users.verify_token(token) is None


def test_rights_admin_includes_everything():
    assert ADA.can("benchmarks") and not ADA.can("runs") and ADA.can(None)
    assert ROOT.can("runs") and ROOT.can("blackboard")


def test_endpoints_need_the_token_and_the_right():
    assert client.get("/benchmarks-only").status_code == 401
    assert client.get("/benchmarks-only", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/benchmarks-only", headers=bearer(BOB)).status_code == 403
    assert client.get("/benchmarks-only", headers=bearer(ADA)).status_code == 200
    assert client.get("/benchmarks-only", headers=bearer(ROOT)).status_code == 200
    assert client.get("/who").json() == {"email": None}
    assert client.get("/who", headers=bearer(BOB)).json() == {"email": "bob@example.org"}


def test_login_issues_a_personal_token():
    ok = client.post("/auth/token", json={"username": "Ada@Example.org", "password": "secret"})
    assert ok.status_code == 200
    assert users.verify_token(ok.json()["token"]) == ADA
    for body in ({"username": "ada@example.org", "password": "wrong"},
                 {"username": "nobody@example.org", "password": "secret"}):
        assert client.post("/auth/token", json=body).status_code == 401
    status = client.get("/auth/status", headers={"Authorization": f"Bearer {ok.json()['token']}"}).json()
    assert status["email"] == "ada@example.org" and status["rights"] == ["benchmarks"]
    assert client.get("/auth/status").status_code == 401


def test_the_pseudonym_carries_only_the_id_never_name_or_email():
    assert users.pseudonym(ADA) == "episerve_user_1" and users.pseudonym(18) == "episerve_user_18"
    for text in (ADA.name, ADA.email):
        assert text not in users.pseudonym(ADA)


def test_the_prefect_run_carries_the_executor_pseudonym_only_when_there_is_one(monkeypatch):
    from app.clients import prefect as prefect_client
    sent = []

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"id": "abc", "state": {"type": "SCHEDULED"}}

    monkeypatch.setattr(prefect_client, "_deployment_id", lambda name: "dep")
    monkeypatch.setattr(prefect_client, "_headers", lambda: {})
    monkeypatch.setattr(prefect_client.settings, "prefect_api_url", "http://prefect")
    monkeypatch.setattr(prefect_client.httpx, "post", lambda url, headers, content: sent.append(json.loads(content)) or Response())
    prefect_client.trigger_model_run([["u", "f"]], "image", "{}", executed_by="episerve_user_18")
    prefect_client.trigger_model_run([["u", "f"]], "image", "{}")
    assert sent[0]["parameters"]["executed_by"] == "episerve_user_18"
    assert "executed_by" not in sent[1]["parameters"]

