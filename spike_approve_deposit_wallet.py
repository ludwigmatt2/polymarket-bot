"""
Decisive experiment, step 5 — approve pUSD from the deposit wallet to the V2
exchanges, via the relayer (the deposit wallet is a contract; only its owner can
make it call approve, and the relayer submits that gaslessly).

Run AFTER the wallet holds pUSD. Sets max pUSD allowance for exchange_v2 and
neg_risk_exchange_v2 in one batch. Needs the same builder creds as deploy.

Run:
  cd /opt/polymarket-bot && venv/bin/python spike_approve_deposit_wallet.py
"""

from __future__ import annotations

import os
import time

from dotenv import load_dotenv
from eth_abi import encode
from eth_utils import keccak, to_checksum_address

load_dotenv()

DEPOSIT_WALLET = "0xCeE18163EEb650177161a7174b760cf71D45bc8a"
PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"           # Polymarket USD (collateral, ERC-20)
CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"            # ConditionalTokens (ERC-1155)
EXCHANGE_V2 = "0xE111180000d2663C0091e4f400237545B87B996B"
NEG_RISK_EXCHANGE_V2 = "0xe2222d279d744050d28e00520010520000310F59"
NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
SPENDERS = [EXCHANGE_V2, NEG_RISK_EXCHANGE_V2, NEG_RISK_ADAPTER]
MAX_UINT = (1 << 256) - 1
MAINNET_RELAYER = "https://relayer-v2.polymarket.com/"


def _approve_calldata(spender: str, amount: int) -> str:
    selector = keccak(text="approve(address,uint256)")[:4]
    return "0x" + (selector + encode(["address", "uint256"], [to_checksum_address(spender), amount])).hex()


def _set_approval_for_all_calldata(operator: str, approved: bool) -> str:
    selector = keccak(text="setApprovalForAll(address,bool)")[:4]
    return "0x" + (selector + encode(["address", "bool"], [to_checksum_address(operator), approved])).hex()


def run() -> None:
    try:
        from py_builder_relayer_client.client import RelayClient
        from py_builder_relayer_client.models import DepositWalletCall
        from py_builder_signing_sdk.config import BuilderConfig, BuilderApiKeyCreds
    except ImportError:
        print("❌ pip install py-builder-relayer-client first."); return

    pk = os.getenv("POLYMARKET_PRIVATE_KEY") or os.getenv("PK")
    bkey, bsecret, bpass = os.getenv("BUILDER_API_KEY"), os.getenv("BUILDER_SECRET"), os.getenv("BUILDER_PASS_PHRASE")
    if not (pk and bkey and bsecret and bpass):
        print("❌ Missing POLYMARKET_PRIVATE_KEY or BUILDER_* creds."); return

    wallet = os.getenv("DEPOSIT_WALLET_ADDRESS", DEPOSIT_WALLET)
    relayer_url = os.getenv("RELAYER_URL", MAINNET_RELAYER)
    chain_id = int(os.getenv("CHAIN_ID", "137"))

    print("\n=== APPROVE pUSD -> V2 exchanges (via relayer) ===\n")
    print(f"Deposit wallet : {wallet}")
    print(f"pUSD           : {PUSD}")

    builder_config = BuilderConfig(
        local_builder_creds=BuilderApiKeyCreds(key=bkey, secret=bsecret, passphrase=bpass)
    )
    client = RelayClient(relayer_url, chain_id, pk, builder_config)

    calls = []
    # pUSD (ERC-20) max-approve to all three exchange contracts — needed to BUY.
    for spender in SPENDERS:
        calls.append(DepositWalletCall(target=to_checksum_address(PUSD), value="0",
                                       data=_approve_calldata(spender, MAX_UINT)))
    # CTF (ERC-1155) setApprovalForAll to the same — needed to SELL/redeem outcome tokens.
    for operator in SPENDERS:
        calls.append(DepositWalletCall(target=to_checksum_address(CTF), value="0",
                                       data=_set_approval_for_all_calldata(operator, True)))
    print("Approving pUSD (buy) + CTF setApprovalForAll (sell) for exchange_v2,")
    print("neg_risk_exchange_v2, neg_risk_adapter — 6 calls in one batch...")

    resp = client.execute_deposit_wallet_batch(
        calls=calls,
        wallet_address=wallet,
        nonce=os.getenv("DEPOSIT_WALLET_NONCE", "0"),
        deadline=os.getenv("DEPOSIT_WALLET_DEADLINE", str(int(time.time()) + 240)),
    )
    print(f"  submitted: {resp}")
    try:
        print(f"  result: {resp.wait()}")
    except Exception as e:  # noqa: BLE001
        print(f"  wait() failed (check status manually): {e}")

    print("\nNext: re-run spike_poly1271_probe.py — should pass the balance/allowance gate.")


if __name__ == "__main__":
    run()
