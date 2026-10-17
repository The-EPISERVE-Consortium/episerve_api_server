"""send_mail against a fake AgentMail (no network)."""

import requests

from app import mailer
from app.config import settings


class _Response:
    def __init__(self, status=200):
        self.status = status

    def raise_for_status(self):
        if self.status >= 400:
            raise requests.HTTPError("boom")


def _configure(monkeypatch):
    monkeypatch.setattr(settings, "agentmail_api_key", "key")
    monkeypatch.setattr(settings, "agentmail_inbox", "episerve@agentmail.to")


def test_send_posts_to_the_inbox_with_the_bearer_key(monkeypatch):
    _configure(monkeypatch)
    calls = []
    monkeypatch.setattr(mailer.requests, "post", lambda url, **kw: calls.append((url, kw)) or _Response())
    assert mailer.send_mail("a@example.org", "Hi", "Text")
    url, kw = calls[0]
    assert url == "https://api.agentmail.to/v0/inboxes/episerve@agentmail.to/messages/send"
    assert kw["json"] == {"to": "a@example.org", "subject": "Hi", "text": "Text"}
    assert kw["headers"] == {"Authorization": "Bearer key"}


def test_send_without_configuration_or_on_error_returns_false_without_raising(monkeypatch):
    monkeypatch.setattr(settings, "agentmail_api_key", "")
    monkeypatch.setattr(mailer.requests, "post", lambda *a, **k: (_ for _ in ()).throw(AssertionError("sent")))
    assert mailer.send_mail("a@example.org", "Hi", "Text") is False
    _configure(monkeypatch)
    monkeypatch.setattr(mailer.requests, "post", lambda *a, **k: _Response(500))
    assert mailer.send_mail("a@example.org", "Hi", "Text") is False
    monkeypatch.setattr(mailer.requests, "post", lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError()))
    assert mailer.send_mail("a@example.org", "Hi", "Text") is False
