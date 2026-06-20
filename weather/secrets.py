"""
Encrypted-at-rest storage for per-user Polymarket credentials.

Backend priority:
  1. keyring  — OS keychain (macOS Keychain, GNOME Keyring, Windows Credential Locker)
  2. fernet   — AES-128 symmetric encryption; key read from POLYMARKET_SECRETS_KEY in .env
     Blobs stored in config/user_keys.enc.json

Each user's creds are stored as an encrypted JSON object:
  {"pk": "0x...", "proxy_address": "0x...", "signature_type": "gnosis-safe",
   "clob_api_key": "...", "clob_secret": "...", "clob_passphrase": "..."}

All fields except `pk` are optional.  Legacy blobs (plain encrypted pk strings
written by an older version) are detected and read back as {"pk": <value>}.
"""

from __future__ import annotations

import functools
import json
import os
from pathlib import Path

from ._io import atomic_write_json

_SERVICE_NAME = "polymarket-bot"
# DATA_DIR lets Railway (or any deployment) point to a persistent volume.
_DATA_DIR = Path(os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or Path(__file__).parent.parent)
_ENC_KEYS_FILE = _DATA_DIR / "config" / "user_keys.enc.json"


@functools.lru_cache(maxsize=1)
def _get_keyring():
    # Skip keyring when a Fernet key is configured — Fernet is portable across
    # all environments (including headless Linux containers on Railway).
    if os.environ.get("POLYMARKET_SECRETS_KEY", "").strip():
        return None
    try:
        import keyring as _kr
        # Detect the no-op fail backend (installed but no daemon running).
        from keyring.backends.fail import Keyring as _FailBackend
        if isinstance(_kr.get_keyring(), _FailBackend):
            return None
        return _kr
    except Exception:
        return None


@functools.lru_cache(maxsize=1)
def _get_fernet():
    key = os.environ.get("POLYMARKET_SECRETS_KEY", "").strip()
    if not key:
        return None
    try:
        from cryptography.fernet import Fernet
        return Fernet(key.encode())
    except Exception:
        return None


# ── Encoding helpers ──────────────────────────────────────────────────────────

def _encode(f, data: dict) -> str:
    return f.encrypt(json.dumps(data, separators=(",", ":")).encode()).decode()


def _decode(f, blob: str) -> dict:
    raw = f.decrypt(blob.encode()).decode()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"pk": raw}  # legacy: plain encrypted pk string


def _decode_keyring(raw: str) -> dict:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"pk": raw}  # legacy


# ── Public API ────────────────────────────────────────────────────────────────

def set_user_creds(uid: int, **fields) -> None:
    """Store or merge-update credential fields for uid.

    Recognised fields: pk, proxy_address, signature_type,
    clob_api_key, clob_secret, clob_passphrase.
    None values are ignored (do not overwrite existing).
    """
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
    _ENC_KEYS_FILE.parent.mkdir(exist_ok=True)
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

    Signs a ClobAuth EIP-712 message with the private key and calls
    POST https://clob.polymarket.com/auth/api-key to get (or re-derive)
    the deterministic L2 credentials tied to this key.

    Returns {"clob_api_key": ..., "clob_secret": ..., "clob_passphrase": ...}.
    Raises RuntimeError on network or auth failure.
    """
    import json as _json
    import time
    import urllib.request
    import urllib.error
    from eth_account import Account
    from eth_account.messages import encode_typed_data

    account = Account.from_key(pk)
    ts = str(int(time.time()))
    nonce = 0

    msg = encode_typed_data(
        domain_data={"name": "ClobAuthDomain", "version": "1", "chainId": 137},
        message_types={
            "ClobAuth": [
                {"name": "key", "type": "string"},
                {"name": "value", "type": "string"},
            ]
        },
        message_data={
            "key": "user",
            "value": _json.dumps({"timestamp": ts, "nonce": nonce}, separators=(",", ":")),
        },
    )
    signed = account.sign_message(msg)
    sig = "0x" + signed.signature.hex()

    req = urllib.request.Request(
        "https://clob.polymarket.com/auth/api-key",
        data=b"",
        method="POST",
        headers={
            "POLY_ADDRESS": account.address,
            "POLY_SIGNATURE": sig,
            "POLY_TIMESTAMP": ts,
            "POLY_NONCE": str(nonce),
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = _json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        raise RuntimeError(f"CLOB auth failed ({exc.code}): {body}") from None

    return {
        "clob_api_key": data["apiKey"],
        "clob_secret": data["secret"],
        "clob_passphrase": data["passphrase"],
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


# ── Backward-compat wrappers ──────────────────────────────────────────────────

def set_user_key(uid: int, pk: str) -> None:
    """Store a private key for uid. Existing proxy/sig fields are preserved."""
    set_user_creds(uid, pk=pk)


def get_user_key(uid: int) -> str | None:
    """Return stored private key, or None."""
    creds = get_user_creds(uid)
    return creds.get("pk") if creds else None
