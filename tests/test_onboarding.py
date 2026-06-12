"""Tests for telegram_onboarding pure helpers — invites, key validation, wallet gen."""

import json
from datetime import datetime, timedelta

import pytest

import telegram_onboarding as ob


@pytest.fixture
def invites_file(tmp_path, monkeypatch):
    f = tmp_path / "invites.json"
    monkeypatch.setattr(ob, "INVITES_FILE", f)
    return f


class TestInvites:
    def test_create_and_validate(self, invites_file):
        code = ob.create_invite(created_by=111, role="viewer")
        inv = ob.validate_invite(code)
        assert inv is not None
        assert inv["role"] == "viewer"
        assert inv["created_by"] == 111

    def test_single_use(self, invites_file):
        code = ob.create_invite(created_by=111)
        ob.mark_invite_used(code, uid=222)
        assert ob.validate_invite(code) is None

    def test_expired_code_rejected(self, invites_file):
        code = ob.create_invite(created_by=111)
        invites = json.loads(invites_file.read_text())
        invites[code]["expires_at"] = (datetime.utcnow() - timedelta(days=1)).isoformat()
        invites_file.write_text(json.dumps(invites))
        assert ob.validate_invite(code) is None

    def test_unknown_code_rejected(self, invites_file):
        assert ob.validate_invite("nope") is None

    def test_corrupt_file_treated_as_empty(self, invites_file):
        invites_file.write_text("{broken")
        assert ob.validate_invite("anything") is None
        # and creating a new invite still works
        code = ob.create_invite(created_by=111)
        assert ob.validate_invite(code) is not None

    def test_codes_are_unique_and_urlsafe(self, invites_file):
        codes = {ob.create_invite(created_by=1) for _ in range(20)}
        assert len(codes) == 20
        assert all("/" not in c and "+" not in c for c in codes)


class TestKeyValidation:
    # well-known anvil/hardhat test key — safe to embed
    VALID = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"

    def test_valid_key_with_prefix(self):
        assert ob.normalize_private_key(self.VALID) == self.VALID

    def test_valid_key_without_prefix(self):
        assert ob.normalize_private_key(self.VALID[2:]) == self.VALID

    def test_whitespace_stripped(self):
        assert ob.normalize_private_key(f"  {self.VALID}\n") == self.VALID

    def test_garbage_rejected(self):
        assert ob.normalize_private_key("not-a-key") is None
        assert ob.normalize_private_key("0x1234") is None
        assert ob.normalize_private_key("") is None


class TestAddressValidation:
    def test_valid_address(self):
        assert ob.is_eth_address("0x" + "ab" * 20)

    def test_invalid(self):
        assert not ob.is_eth_address("0x123")          # too short
        assert not ob.is_eth_address("ab" * 21)        # no 0x
        assert not ob.is_eth_address("0x" + "zz" * 20) # not hex


class TestWalletGeneration:
    def test_generates_valid_keypair(self):
        address, pk = ob.generate_wallet()
        assert ob.is_eth_address(address)
        normalized = ob.normalize_private_key(pk)
        assert normalized is not None
        # key must re-derive the same address
        from eth_account import Account
        assert Account.from_key(normalized).address == address

    def test_wallets_are_unique(self):
        addrs = {ob.generate_wallet()[0] for _ in range(5)}
        assert len(addrs) == 5
