"""
Encrypted-at-rest storage for per-user Polymarket private keys.

Backend priority:
  1. keyring  — OS keychain (macOS Keychain, GNOME Keyring, Windows Credential Locker)
  2. fernet   — AES-128 symmetric encryption; key read from POLYMARKET_SECRETS_KEY in .env
     Blobs stored in config/user_keys.enc.json

Both backends are optional imports; whichever is present is used.
set_user_key / get_user_key raise RuntimeError when neither backend is available.
"""

from __future__ import annotations

import functools
import json
import os
from pathlib import Path

from ._io import atomic_write_json

_SERVICE_NAME = "polymarket-bot"
# DATA_DIR lets Railway (or any deployment) point to a persistent volume.
# Defaults to the project root so local behaviour is unchanged.
_DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).parent.parent))
_ENC_KEYS_FILE = _DATA_DIR / "config" / "user_keys.enc.json"


@functools.lru_cache(maxsize=1)
def _get_keyring():
    """Return the keyring module if available, else None. Cached per process."""
    try:
        import keyring as _kr
        return _kr
    except Exception:
        return None


@functools.lru_cache(maxsize=1)
def _get_fernet():
    """Return a Fernet instance if POLYMARKET_SECRETS_KEY is set, else None. Cached per process."""
    key = os.environ.get("POLYMARKET_SECRETS_KEY", "").strip()
    if not key:
        return None
    try:
        from cryptography.fernet import Fernet
        return Fernet(key.encode())
    except Exception:
        return None


def set_user_key(uid: int, pk: str) -> None:
    """Store a private key for the given user ID, encrypted at rest."""
    kr = _get_keyring()
    if kr is not None:
        kr.set_password(_SERVICE_NAME, f"uid-{uid}", pk)
        return

    f = _get_fernet()
    if f is None:
        raise RuntimeError(
            "No encrypted key storage available. "
            "Install 'keyring' or set POLYMARKET_SECRETS_KEY in .env."
        )
    blob = f.encrypt(pk.encode()).decode()
    _ENC_KEYS_FILE.parent.mkdir(exist_ok=True)
    data: dict = {}
    if _ENC_KEYS_FILE.exists():
        try:
            data = json.loads(_ENC_KEYS_FILE.read_text())
        except Exception:
            pass
    data[str(uid)] = blob
    atomic_write_json(_ENC_KEYS_FILE, data)


def get_user_key(uid: int) -> str | None:
    """Retrieve a stored private key, or None if not found."""
    kr = _get_keyring()
    if kr is not None:
        val = kr.get_password(_SERVICE_NAME, f"uid-{uid}")
        if val is not None:
            return val

    f = _get_fernet()
    if f is not None and _ENC_KEYS_FILE.exists():
        try:
            data = json.loads(_ENC_KEYS_FILE.read_text())
            blob = data.get(str(uid))
            if blob:
                return f.decrypt(blob.encode()).decode()
        except Exception:
            pass

    return None
