"""Tests for the boot-time security self-check (SECURITY_PLAN Phase F3)."""

import os

import pytest


@pytest.fixture
def tb():
    os.environ.setdefault("POLYMARKET_BOT_TOKEN", "test:token")
    os.environ.setdefault("TELEGRAM_ADMIN_ID", "1")
    import telegram_bot as _tb
    return _tb


def test_env_only_key_is_flagged(tb, monkeypatch):
    import weather.secrets as s
    monkeypatch.setattr(s, "get_user_creds", lambda uid: {"pk": "x"})
    monkeypatch.setenv("POLYMARKET_SECRETS_KEY", "k")
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    issues = tb._security_self_check()
    assert any("master key" in i for i in issues)


def test_credential_key_not_flagged(tb, monkeypatch, tmp_path):
    import weather.secrets as s
    monkeypatch.setattr(s, "get_user_creds", lambda uid: {"pk": "x"})
    monkeypatch.delenv("POLYMARKET_SECRETS_KEY", raising=False)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    issues = tb._security_self_check()
    assert not any("master key" in i for i in issues)


def test_owner_without_creds_is_flagged(tb, monkeypatch):
    import weather.secrets as s
    monkeypatch.setattr(s, "get_user_creds", lambda uid: None)
    monkeypatch.delenv("POLYMARKET_SECRETS_KEY", raising=False)
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    issues = tb._security_self_check()
    assert any("no stored key" in i for i in issues)


def test_clean_state_has_no_issues(tb, monkeypatch, tmp_path):
    import weather.secrets as s
    monkeypatch.setattr(s, "get_user_creds", lambda uid: {"pk": "x"})
    monkeypatch.delenv("POLYMARKET_SECRETS_KEY", raising=False)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    # Point the file-perm checks at an empty dir so they find nothing to flag
    # (the real repo .env would otherwise trip this).
    monkeypatch.setattr(tb, "ROOT", tmp_path)
    monkeypatch.setattr(tb, "USERS_FILE", tmp_path / "config" / "users.json")
    assert tb._security_self_check() == []
