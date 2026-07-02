#!/usr/bin/env python3
"""
Rotate the master Fernet key (SECURITY_PLAN Phase G / G1).

Re-encrypts every blob in data/config/user_keys.enc.json from an OLD master key
to a NEW one, so the key that protects all per-user private keys can be rotated
without ever exposing the plaintext keys. The inner plaintext is never parsed —
each Fernet token is simply decrypted with the old key and re-encrypted with the
new one — so legacy blob formats rotate cleanly too.

Usage (run on the VPS, as the service user so it can read the store):
  # rotate, generating a fresh new key (old key taken from the systemd credential
  # or POLYMARKET_SECRETS_KEY env if --old-key is omitted):
  sudo -u bot CREDENTIALS_DIRECTORY=/run/credentials/polymarket-bot.service \
       RAILWAY_VOLUME_MOUNT_PATH=/opt/polymarket-bot/data \
       ./venv/bin/python rotate_secrets_key.py

  # or supply both explicitly:
  ./venv/bin/python rotate_secrets_key.py --old-key <OLD> --new-key <NEW>

It backs up the store to *.pre-rotate-<ts> first and writes the new store 0600.
After it prints the NEW key: install that key at /etc/polymarket-bot/secrets_key
(root 600), restart the service, verify decryption, THEN destroy the old key.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

sys.path.insert(0, str(Path(__file__).parent))
from weather.paths import DATA_DIR  # noqa: E402

DEFAULT_STORE = DATA_DIR / "config" / "user_keys.enc.json"


def resolve_old_key(arg: str | None) -> str:
    if arg:
        return arg.strip()
    cred = os.environ.get("CREDENTIALS_DIRECTORY")
    if cred:
        p = Path(cred) / "polymarket_secrets_key"
        if p.exists():
            return p.read_text().strip()
    return os.environ.get("POLYMARKET_SECRETS_KEY", "").strip()


def main() -> None:
    ap = argparse.ArgumentParser(description="Rotate the master Fernet key.")
    ap.add_argument("--old-key", help="current master key (default: systemd cred / env)")
    ap.add_argument("--new-key", help="new master key (default: generate a fresh one)")
    ap.add_argument("--store", default=str(DEFAULT_STORE), help="path to user_keys.enc.json")
    args = ap.parse_args()

    old = resolve_old_key(args.old_key)
    if not old:
        sys.exit("ERROR: no OLD key (pass --old-key or set the credential / env var).")
    new = (args.new_key or Fernet.generate_key().decode()).strip()
    try:
        f_old, f_new = Fernet(old.encode()), Fernet(new.encode())
    except Exception as e:  # noqa: BLE001
        sys.exit(f"ERROR: invalid Fernet key ({e}).")

    store_path = Path(args.store)
    if not store_path.exists():
        sys.exit(f"ERROR: store not found: {store_path}")
    store = json.loads(store_path.read_text())

    out: dict[str, str] = {}
    for uid, token in store.items():
        try:
            plain = f_old.decrypt(token.encode())
        except InvalidToken:
            sys.exit(f"ERROR: OLD key cannot decrypt uid {uid} — wrong old key; aborting (no changes written).")
        out[uid] = f_new.encrypt(plain).decode()

    backup = store_path.with_name(store_path.name + f".pre-rotate-{int(time.time())}")
    shutil.copy2(store_path, backup)
    os.chmod(backup, 0o600)

    tmp = store_path.with_suffix(store_path.suffix + ".tmp")
    tmp.write_text(json.dumps(out, indent=2))
    os.chmod(tmp, 0o600)
    os.replace(tmp, store_path)

    print(f"✅ Rotated {len(out)} blob(s). Backup: {backup}")
    print("NEW MASTER KEY — store it securely, install as the systemd credential, then destroy the old key:")
    print(new)


if __name__ == "__main__":
    main()
