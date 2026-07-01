"""
Encrypted-at-rest storage for per-user Polymarket credentials.

Backend priority:
  1. keyring  — OS keychain (macOS Keychain, GNOME Keyring, Windows Credential Locker)
  2. fernet   — AES-128 symmetric encryption; master key from a systemd
                LoadCredential ($CREDENTIALS_DIRECTORY) if present, else POLYMARKET_SECRETS_KEY
     Blobs stored in config/user_keys.enc.json

Each user's creds are stored as an encrypted JSON object:
  {"pk": "0x...", "funder_address": "0x...", "signature_type": 1,
   "clob_api_key": "...", "clob_secret": "...", "clob_passphrase": "..."}

signature_type integers match the official Polymarket SDK:
  0 = EOA (standard wallet — fresh accounts, signing key IS the funder)
  1 = POLY_PROXY (email/Google Polymarket login — proxy wallet flow)
  2 = GNOSIS_SAFE (actual Gnosis Safe multisig)
  3 = POLY_1271 (deposit wallet — recommended for new API users)

All fields except `pk` are optional.  Legacy blobs using the old pmxt string
format ("eoa", "gnosis-safe") and "proxy_address" key are auto-migrated on read.
"""

from __future__ import annotations

import functools
import json
import os
from pathlib import Path

from ._io import atomic_write_json
from .paths import DATA_DIR as _DATA_DIR

_SERVICE_NAME = "polymarket-bot"
_ENC_KEYS_FILE = _DATA_DIR / "config" / "user_keys.enc.json"

# Maps legacy pmxt string signature types → official SDK integers.
# "gnosis-safe" was pmxt's name for the standard Polymarket proxy wallet (email/Google login),
# which the official SDK calls POLY_PROXY (1).
_LEGACY_SIG_MAP: dict[str, int] = {
    "eoa": 0,
    "poly-proxy": 1,
    "poly_proxy": 1,
    "gnosis-safe": 1,   # pmxt used this for email/Google Polymarket proxy accounts
    "gnosis_safe": 2,   # actual Gnosis Safe multisig → GNOSIS_SAFE
    "poly-1271": 3,
    "poly_1271": 3,
}


def _migrate_legacy_creds(creds: dict) -> dict:
    """Convert old pmxt-style field names and values to the current schema."""
    # proxy_address → funder_address
    if "proxy_address" in creds and "funder_address" not in creds:
        creds["funder_address"] = creds.pop("proxy_address")
    # string signature_type → integer
    sig = creds.get("signature_type")
    if isinstance(sig, str):
        creds["signature_type"] = _LEGACY_SIG_MAP.get(sig.lower(), 0)
    return creds


def _secrets_key() -> str:
    """Return the master Fernet key.

    Priority (SECURITY_PLAN Phase E):
      1. systemd LoadCredential — read from $CREDENTIALS_DIRECTORY/polymarket_secrets_key.
         Keeps the key out of the app's .env and off the app dir; systemd exposes it
         to this service only, via a private tmpfs.
      2. POLYMARKET_SECRETS_KEY env var (bootstrap / non-systemd environments).
    Read fresh (not cached) so a rotation via service restart is picked up.
    """
    cred_dir = os.environ.get("CREDENTIALS_DIRECTORY")
    if cred_dir:
        try:
            val = (Path(cred_dir) / "polymarket_secrets_key").read_text().strip()
            if val:
                return val
        except OSError:
            pass
    return os.environ.get("POLYMARKET_SECRETS_KEY", "").strip()


@functools.lru_cache(maxsize=1)
def _get_keyring():
    # Skip keyring when a Fernet key is configured — Fernet is portable across
    # all environments (including headless Linux containers / systemd services).
    if _secrets_key():
        return None
    try:
        import keyring as _kr
        try:
            from keyring.backends.fail import Keyring as _FailBackend
            if isinstance(_kr.get_keyring(), _FailBackend):
                return None
        except Exception:
            pass  # submodule import may fail when keyring is mocked in tests
        return _kr
    except ImportError:
        return None


@functools.lru_cache(maxsize=1)
def _get_fernet():
    key = _secrets_key()
    if not key:
        return None
    try:
        from cryptography.fernet import Fernet
        return Fernet(key.encode())
    except Exception as exc:
        raise RuntimeError(
            "POLYMARKET_SECRETS_KEY is set but is not a valid Fernet key "
            f"({exc.__class__.__name__}). Generate one with: python -c "
            "\"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        ) from exc


# ── Encoding helpers ──────────────────────────────────────────────────────────

def _encode(f, data: dict) -> str:
    return f.encrypt(json.dumps(data, separators=(",", ":")).encode()).decode()


def _decode(f, blob: str) -> dict:
    raw = f.decrypt(blob.encode()).decode()
    try:
        return _migrate_legacy_creds(json.loads(raw))
    except json.JSONDecodeError:
        return {"pk": raw}  # legacy: plain encrypted pk string


def _decode_keyring(raw: str) -> dict:
    try:
        return _migrate_legacy_creds(json.loads(raw))
    except json.JSONDecodeError:
        return {"pk": raw}  # legacy


# ── Public API ────────────────────────────────────────────────────────────────

def set_user_creds(uid: int, **fields) -> None:
    """Store or merge-update credential fields for uid.

    Recognised fields: pk, funder_address, signature_type (int),
    clob_api_key, clob_secret, clob_passphrase.
    None values are ignored (do not overwrite existing).
    """
    # Migrate any legacy field names passed by callers
    if "proxy_address" in fields:
        fields.setdefault("funder_address", fields.pop("proxy_address"))
    sig = fields.get("signature_type")
    if isinstance(sig, str):
        fields["signature_type"] = _LEGACY_SIG_MAP.get(sig.lower(), 0)

    kr = _get_keyring()
    if kr is not None:
        existing_raw = kr.get_password(_SERVICE_NAME, f"uid-{uid}")
        existing = _decode_keyring(existing_raw) if existing_raw else {}
        existing.update({k: v for k, v in fields.items() if v is not None})
        kr.set_password(_SERVICE_NAME, f"uid-{uid}",
                        json.dumps(existing, separators=(",", ":")))
        return

    f = _get_fernet()
    if f is None:
        raise RuntimeError(
            "No encrypted key storage available. "
            "Install 'keyring' or set POLYMARKET_SECRETS_KEY in .env."
        )
    _ENC_KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
    store: dict = {}
    if _ENC_KEYS_FILE.exists():
        try:
            store = json.loads(_ENC_KEYS_FILE.read_text())
        except Exception:
            pass
    uid_str = str(uid)
    existing: dict = {}
    if uid_str in store:
        try:
            existing = _decode(f, store[uid_str])
        except Exception:
            pass
    existing.update({k: v for k, v in fields.items() if v is not None})
    store[uid_str] = _encode(f, existing)
    atomic_write_json(_ENC_KEYS_FILE, store)


def get_user_creds(uid: int) -> dict | None:
    """Return decrypted creds dict, or None if uid not found."""
    kr = _get_keyring()
    if kr is not None:
        raw = kr.get_password(_SERVICE_NAME, f"uid-{uid}")
        if raw is not None:
            return _decode_keyring(raw)

    f = _get_fernet()
    if f is not None and _ENC_KEYS_FILE.exists():
        try:
            store = json.loads(_ENC_KEYS_FILE.read_text())
            blob = store.get(str(uid))
            if blob:
                return _decode(f, blob)
        except Exception:
            pass

    return None


# ── L2 credential derivation ─────────────────────────────────────────────────

def derive_clob_creds(pk: str) -> dict:
    """Derive L2 CLOB API credentials from an L1 private key.

    Uses the official py-clob-client-v2 SDK which signs the correct EIP-712
    ClobAuth struct and calls POST https://clob.polymarket.com/auth/api-key.

    Returns {"clob_api_key": ..., "clob_secret": ..., "clob_passphrase": ...}.
    Raises RuntimeError on network or auth failure.
    """
    try:
        from py_clob_client_v2 import ClobClient
    except ImportError as exc:
        raise RuntimeError(
            "py-clob-client-v2 is required for credential derivation. "
            "Run: pip install py-clob-client-v2"
        ) from exc

    try:
        client = ClobClient(
            host="https://clob.polymarket.com",
            chain_id=137,
            key=pk,
        )
        creds = client.create_or_derive_api_key()
    except Exception as exc:
        raise RuntimeError(f"CLOB auth failed: {exc}") from exc

    return {
        "clob_api_key": creds.api_key,
        "clob_secret": creds.api_secret,
        "clob_passphrase": creds.api_passphrase,
    }


def derive_and_store_clob_creds(uid: int) -> dict:
    """Derive L2 CLOB credentials for uid and persist them in the encrypted store.

    Requires pk to already be stored for uid.
    Returns the derived creds dict.
    Raises RuntimeError if pk is missing or derivation fails.
    """
    creds = get_user_creds(uid)
    if not creds or not creds.get("pk"):
        raise RuntimeError(f"No private key stored for uid={uid}")
    l2 = derive_clob_creds(creds["pk"])
    set_user_creds(uid, **l2)
    return l2


def enable_deposit_wallet(uid: int) -> dict:
    """Switch uid to the V2 deposit-wallet flow: derive the deterministic deposit
    wallet from the stored pk's EOA, store it as funder_address, and set
    signature_type=3 (POLY_1271). Idempotent. Returns
    {"funder_address","signature_type","deployed"}.

    The deposit wallet must be DEPLOYED on-chain (via the relayer) before live
    orders fill — this only wires the creds. `deployed` flags current state.
    """
    creds = get_user_creds(uid)
    if not creds or not creds.get("pk"):
        raise RuntimeError(f"No private key stored for uid={uid}")
    from eth_account import Account
    from . import relayer
    eoa = Account.from_key(creds["pk"]).address
    wallet = relayer.derive_deposit_wallet(eoa)
    if not wallet:
        raise RuntimeError("Could not derive deposit wallet (factory unreachable)")
    set_user_creds(uid, funder_address=wallet, signature_type=3)

    # Ensure L2 CLOB creds exist (EOA-derived, sig-independent) — without them
    # every authenticated call fails and live trading is silently skipped.
    clob_ready = bool(creds.get("clob_api_key"))
    if not clob_ready:
        try:
            derive_and_store_clob_creds(uid)  # single home for derive + store
            clob_ready = True
        except Exception:
            clob_ready = False  # best-effort; caller can retry derive_and_store_clob_creds

    return {
        "funder_address": wallet,
        "signature_type": 3,
        "deployed": relayer.is_deployed(wallet),
        "clob_ready": clob_ready,
    }


def prepare_for_live(uid: int) -> dict:
    """Make a user's deposit wallet trade-ready at go-live (all gasless via the
    relayer): deploy it if needed, wrap any USDC.e → pUSD, and approve the V2
    exchanges once. Idempotent — safe to call on every go-live. Returns
    {"ready": bool, "pusd": float, "deployed": bool, "error"?: str}.

    Requires the user already wired via enable_deposit_wallet (funder + sig=3).
    """
    creds = get_user_creds(uid)
    if not creds or not creds.get("pk") or not creds.get("funder_address"):
        return {"ready": False, "pusd": 0.0, "deployed": False,
                "error": "no deposit wallet configured"}
    wallet = creds["funder_address"]
    from . import relayer
    rc = relayer.RelayerClient(pk=creds["pk"])
    deployed = relayer.is_deployed(wallet)
    error = None
    try:
        if not deployed:
            rc.deploy_deposit_wallet(wallet)
            deployed = True
        usdce = relayer.usdce_balance(wallet)
        if usdce > 0:
            rc.wrap_usdce_to_pusd(wallet, int(usdce * 1_000_000))  # floor: never over-wrap
        if not creds.get("exchanges_approved"):
            rc.approve_exchanges(wallet)
            set_user_creds(uid, exchanges_approved=True)
    except Exception as e:  # noqa: BLE001
        error = str(e)
    pusd = relayer.pusd_balance(wallet)  # read once, shared by both outcomes
    result = {"ready": error is None and pusd > 0, "pusd": pusd, "deployed": deployed}
    if error:
        result["error"] = error
    return result


# ── Backward-compat wrappers ──────────────────────────────────────────────────

def set_user_key(uid: int, pk: str) -> None:
    """Store a private key for uid. Existing funder/sig fields are preserved."""
    set_user_creds(uid, pk=pk)


def get_user_key(uid: int) -> str | None:
    """Return stored private key, or None."""
    creds = get_user_creds(uid)
    return creds.get("pk") if creds else None
