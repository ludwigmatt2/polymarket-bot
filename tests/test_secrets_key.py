"""Tests for master-key resolution — systemd credential vs env (SECURITY_PLAN Phase E)."""

import importlib

import pytest


@pytest.fixture
def sec(monkeypatch):
    import weather.secrets as _sec
    importlib.reload(_sec)
    # Clear the lru_caches so each test resolves the key fresh.
    _sec._get_fernet.cache_clear()
    _sec._get_keyring.cache_clear()
    return _sec


def test_env_key_used_when_no_credential(sec, monkeypatch):
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    monkeypatch.setenv("POLYMARKET_SECRETS_KEY", "env-key")
    assert sec._secrets_key() == "env-key"


def test_credential_dir_takes_precedence(sec, monkeypatch, tmp_path):
    (tmp_path / "polymarket_secrets_key").write_text("cred-key\n")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    monkeypatch.setenv("POLYMARKET_SECRETS_KEY", "env-key")
    assert sec._secrets_key() == "cred-key"  # credential wins, whitespace stripped


def test_falls_back_to_env_when_credential_file_missing(sec, monkeypatch, tmp_path):
    # CREDENTIALS_DIRECTORY set but the file isn't there → fall back to env.
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    monkeypatch.setenv("POLYMARKET_SECRETS_KEY", "env-key")
    assert sec._secrets_key() == "env-key"


def test_empty_when_neither_present(sec, monkeypatch):
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    monkeypatch.delenv("POLYMARKET_SECRETS_KEY", raising=False)
    assert sec._secrets_key() == ""


def test_real_fernet_key_from_credential_builds_cipher(sec, monkeypatch, tmp_path):
    from cryptography.fernet import Fernet
    key = Fernet.generate_key().decode()
    (tmp_path / "polymarket_secrets_key").write_text(key)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    monkeypatch.delenv("POLYMARKET_SECRETS_KEY", raising=False)
    sec._get_fernet.cache_clear()
    f = sec._get_fernet()
    assert f is not None
    assert f.decrypt(f.encrypt(b"hi")) == b"hi"
