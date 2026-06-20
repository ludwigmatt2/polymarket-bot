"""Tests for weather/secrets.py — encrypted key storage."""

import os
from unittest.mock import patch, MagicMock

import pytest


class TestSecretsKeyring:
    """Tests using the keyring backend (mocked)."""

    def test_set_get_roundtrip_keyring(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # Patch keyring module to avoid touching the real OS keychain
        mock_kr = MagicMock()
        # get_password returns None on first read (no existing creds), then the stored JSON
        mock_kr.get_password.side_effect = [None, '{"pk":"test-private-key"}']
        with patch.dict("sys.modules", {"keyring": mock_kr}):
            from importlib import reload
            import weather.secrets as sec
            reload(sec)
            sec.set_user_key(42, "test-private-key")
            # Now stores a JSON dict, not a raw pk string
            mock_kr.set_password.assert_called_once_with(
                "polymarket-bot", "uid-42", '{"pk":"test-private-key"}'
            )
            result = sec.get_user_key(42)
            assert result == "test-private-key"

    def test_get_returns_none_if_unset_keyring(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        mock_kr = MagicMock()
        mock_kr.get_password.return_value = None
        with patch.dict("sys.modules", {"keyring": mock_kr}):
            from importlib import reload
            import weather.secrets as sec
            reload(sec)
            result = sec.get_user_key(99)
            assert result is None


class TestSecretsFernet:
    """Tests using the fernet fallback (no keyring)."""

    def _fernet_key(self):
        from cryptography.fernet import Fernet
        return Fernet.generate_key().decode()

    def test_set_get_roundtrip_fernet(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "config").mkdir()
        fernet_key = self._fernet_key()
        monkeypatch.setenv("POLYMARKET_SECRETS_KEY", fernet_key)

        # Patch keyring to be unavailable
        with patch.dict("sys.modules", {"keyring": None}):
            from importlib import reload
            import weather.secrets as sec
            reload(sec)
            sec.set_user_key(7, "my-secret-pk")
            result = sec.get_user_key(7)
            assert result == "my-secret-pk"

    def test_get_returns_none_if_unset_fernet(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "config").mkdir()
        fernet_key = self._fernet_key()
        monkeypatch.setenv("POLYMARKET_SECRETS_KEY", fernet_key)

        with patch.dict("sys.modules", {"keyring": None}):
            from importlib import reload
            import weather.secrets as sec
            reload(sec)
            result = sec.get_user_key(404)
            assert result is None

    def test_set_raises_when_no_backend(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("POLYMARKET_SECRETS_KEY", raising=False)

        with patch.dict("sys.modules", {"keyring": None}):
            from importlib import reload
            import weather.secrets as sec
            reload(sec)
            with pytest.raises(RuntimeError, match="No encrypted key storage"):
                sec.set_user_key(1, "pk")
