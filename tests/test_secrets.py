"""Tests for weather/secrets.py — encrypted key storage."""

import os
from unittest.mock import patch, MagicMock

import pytest


class TestSecretsKeyring:
    """Tests using the keyring backend (mocked)."""

    def test_set_get_roundtrip_keyring(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # Clear Fernet env var so _get_keyring() doesn't skip keyring
        monkeypatch.delenv("POLYMARKET_SECRETS_KEY", raising=False)
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


class TestEnableDepositWallet:
    """Phase 2 — wiring a user onto the V2 deposit-wallet flow."""

    # A throwaway hardhat test key (well-known, no funds) — Account.from_key works offline.
    _TEST_PK = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"

    def test_enable_stores_funder_and_sig3(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "config").mkdir()
        from cryptography.fernet import Fernet
        monkeypatch.setenv("POLYMARKET_SECRETS_KEY", Fernet.generate_key().decode())

        with patch.dict("sys.modules", {"keyring": None}):
            from importlib import reload
            import weather.secrets as sec
            reload(sec)
            sec.set_user_key(55, self._TEST_PK)
            # Mock the on-chain derivation/deploy + L2 derivation (no network in tests)
            import weather.relayer as rl
            monkeypatch.setattr(rl, "derive_deposit_wallet",
                                lambda eoa: "0xDEAD00000000000000000000000000000000bEEF")
            monkeypatch.setattr(rl, "is_deployed", lambda w: True)
            monkeypatch.setattr(sec, "derive_clob_creds",
                                lambda pk: {"clob_api_key": "k", "clob_secret": "s",
                                            "clob_passphrase": "p"})
            res = sec.enable_deposit_wallet(55)
            assert res["signature_type"] == 3
            assert res["funder_address"] == "0xDEAD00000000000000000000000000000000bEEF"
            assert res["deployed"] is True
            assert res["clob_ready"] is True
            creds = sec.get_user_creds(55)
            assert creds["signature_type"] == 3
            assert creds["funder_address"] == "0xDEAD00000000000000000000000000000000bEEF"
            assert creds["pk"] == self._TEST_PK  # pk preserved
            assert creds["clob_api_key"] == "k"  # L2 creds derived + stored

    def test_enable_raises_without_pk(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "config").mkdir()
        from cryptography.fernet import Fernet
        monkeypatch.setenv("POLYMARKET_SECRETS_KEY", Fernet.generate_key().decode())
        with patch.dict("sys.modules", {"keyring": None}):
            from importlib import reload
            import weather.secrets as sec
            reload(sec)
            with pytest.raises(RuntimeError, match="No private key"):
                sec.enable_deposit_wallet(999)
