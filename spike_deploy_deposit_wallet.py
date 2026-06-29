"""
Decisive experiment, step 2 — DEPLOY the V2 deposit wallet via Polymarket's
relayer (the factory's deploy() is onlyOperator; the relayer IS the operator and
pays the gas, so this needs builder creds but ~no funds).

PREREQUISITES (you do these once):
  1. Generate self-serve builder creds at polymarket.com/settings?tab=builder
     (Builders tab). No partnership needed.
  2. pip install py-builder-relayer-client     (into the venv)
  3. Put these in .env (alongside POLYMARKET_PRIVATE_KEY):
        BUILDER_API_KEY=...
        BUILDER_SECRET=...
        BUILDER_PASS_PHRASE=...
     Optional: RELAYER_URL (defaults to mainnet), CHAIN_ID (defaults 137).

Run (VPS or local — deploy isn't geoblocked):
  venv/bin/python spike_deploy_deposit_wallet.py

It cross-checks the derived wallet vs our expected 0xcee1…bc8a, deploys if not
already deployed, and waits for the tx. After this, run spike_clobauth_1271.py.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

EXPECTED_WALLET = "0xcee18163eeb650177161a7174b760cf71d45bc8a"
MAINNET_RELAYER = "https://relayer-v2.polymarket.com/"


def run() -> None:
    try:
        from py_builder_relayer_client.client import RelayClient
        from py_builder_signing_sdk.config import BuilderConfig, BuilderApiKeyCreds
    except ImportError:
        print("❌ pip install py-builder-relayer-client  (into the venv) first.")
        return

    pk = os.getenv("POLYMARKET_PRIVATE_KEY") or os.getenv("PK")
    relayer_url = os.getenv("RELAYER_URL", MAINNET_RELAYER)
    chain_id = int(os.getenv("CHAIN_ID", "137"))
    bkey = os.getenv("BUILDER_API_KEY")
    bsecret = os.getenv("BUILDER_SECRET")
    bpass = os.getenv("BUILDER_PASS_PHRASE")

    if not pk:
        print("❌ POLYMARKET_PRIVATE_KEY not set")
        return
    if not (bkey and bsecret and bpass):
        print("❌ Builder creds missing — set BUILDER_API_KEY/SECRET/PASS_PHRASE.")
        print("   Generate them at polymarket.com/settings?tab=builder")
        return

    print("\n=== DEPLOY DEPOSIT WALLET (via relayer) ===\n")
    print(f"Relayer  : {relayer_url}")
    print(f"Chain    : {chain_id}")

    builder_config = BuilderConfig(
        local_builder_creds=BuilderApiKeyCreds(key=bkey, secret=bsecret, passphrase=bpass)
    )
    client = RelayClient(relayer_url, chain_id, pk, builder_config)

    expected = client.get_expected_deposit_wallet()
    print(f"Expected deposit wallet: {expected}")
    if expected.lower() != EXPECTED_WALLET.lower():
        print(f"⚠️  Differs from our derived {EXPECTED_WALLET} — investigate before proceeding.")

    try:
        if client.get_deployed(expected):
            print("✅ Already deployed — nothing to do. Proceed to spike_clobauth_1271.py")
            return
    except Exception as e:  # noqa: BLE001 — get_deployed is best-effort
        print(f"(deployed-check unavailable, proceeding: {e})")

    print("\nSubmitting deploy request to the relayer...")
    resp = client.deploy_deposit_wallet()
    print(f"  submitted: {resp}")
    try:
        awaited = resp.wait()
        print(f"  result: {awaited}")
    except Exception as e:  # noqa: BLE001
        print(f"  wait() failed (check status manually): {e}")

    print("\nNext: venv/bin/python spike_clobauth_1271.py  (the decisive auth probe)")


if __name__ == "__main__":
    run()
