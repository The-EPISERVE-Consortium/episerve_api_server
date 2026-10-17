"""Tests for the fail-closed secret loading."""

import pytest

from app.secrets import INSECURE_DEFAULTS, require_secret


def test_rejects_unset(monkeypatch):
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    monkeypatch.delenv("ALLOW_INSECURE_DEFAULT_SECRETS", raising=False)
    with pytest.raises(RuntimeError):
        require_secret("SESSION_SECRET")


def test_rejects_shipped_default(monkeypatch):
    for value in INSECURE_DEFAULTS:
        monkeypatch.setenv("SESSION_SECRET", value)
        monkeypatch.delenv("ALLOW_INSECURE_DEFAULT_SECRETS", raising=False)
        with pytest.raises(RuntimeError):
            require_secret("SESSION_SECRET")
