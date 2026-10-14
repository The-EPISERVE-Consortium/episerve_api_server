"""Tests for the fail-closed secret loading."""

import pytest

from app.secrets import INSECURE_DEFAULTS, require_secret


def test_accepts_a_real_secret(monkeypatch):
    monkeypatch.setenv("STORAGE_SECRET", "a-real-random-value")
    monkeypatch.delenv("ALLOW_INSECURE_DEFAULT_SECRETS", raising=False)
    assert require_secret("STORAGE_SECRET") == "a-real-random-value"


def test_rejects_unset(monkeypatch):
    monkeypatch.delenv("STORAGE_SECRET", raising=False)
    monkeypatch.delenv("ALLOW_INSECURE_DEFAULT_SECRETS", raising=False)
    with pytest.raises(RuntimeError):
        require_secret("STORAGE_SECRET")


def test_rejects_shipped_default(monkeypatch):
    for value in INSECURE_DEFAULTS:
        monkeypatch.setenv("STORAGE_SECRET", value)
        monkeypatch.delenv("ALLOW_INSECURE_DEFAULT_SECRETS", raising=False)
        with pytest.raises(RuntimeError):
            require_secret("STORAGE_SECRET")


def test_allows_override_for_local_dev(monkeypatch):
    monkeypatch.delenv("STORAGE_SECRET", raising=False)
    monkeypatch.setenv("ALLOW_INSECURE_DEFAULT_SECRETS", "1")
    assert require_secret("STORAGE_SECRET", dev_default="dev") == "dev"
    monkeypatch.setenv("STORAGE_SECRET", "episerve-dev-secret-change-me")
    assert require_secret("STORAGE_SECRET", dev_default="dev") == "episerve-dev-secret-change-me"
