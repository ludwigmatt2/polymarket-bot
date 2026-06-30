"""
Wrap the deposit wallet's USDC.e into pUSD (V2 collateral), gasless via the
relayer. The deposit wallet itself calls approve(onramp) + onramp.wrap, so no POL
is needed in the wallet — the relayer pays gas.

Onramp wrap verified against bytecode: wrap(address _asset,address _to,uint256
_amount) selector 0x62355638. Reverts are safe (USDC.e stays in the wallet).

Run on the VPS (has builder creds + py-builder-relayer-client):
  cd /opt/polymarket-bot && venv/bin/python spike_wrap_deposit_wallet.py
"""

from __future__ import annotations

import json
import os
import time
import urllib.request

from dotenv import load_dotenv
from eth_abi import encode
from eth_utils import keccak, to_checksum_address

load_dotenv()

DEPOSIT_WALLET = "0xCeE18163EEb650177161a7174b760cf71D45bc8a"
USDCE = "0x2791Bca1f2de4661ED88A30C99a7a9449Aa84174"          # USDC.e (wrapped asset)
PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"           # Polymarket USD
ONRAMP = "0x93070a847efEf7F70739046A929D47a521F5B8ee"          # CollateralOnramp
MAINNET_RELAYER = "https://relayer-v2.polymarket.com/"
RPCS = ["https://polygon.llamarpc.com", "https://1rpc.io/matic",
        "https://polygon-bor-rpc.publicnode.com", "https://rpc.ankr.com/polygon"]


def _onchain_nonce(wallet: str) -> int:
    """Deposit wallet's current on-chain nonce() (selector 0xaffed0e0)."""
    for ep in RPCS:
        try:
            req = urllib.request.Request(ep, data=json.dumps({"jsonrpc": "2.0", "id": 1,
                "method": "eth_call", "params": [{"to": wallet, "data": "0xaffed0e0"}, "latest"]}).encode(),
                headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=12) as r:
                res = json.loads(r.read()).get("result")
            if res and res != "0x":
                return int(res, 16)
        except Exception:
            pass
    return 0


def _bal(token: str, holder: str) -> int:
    data = "0x70a08231" + holder.lower().replace("0x", "").zfill(64)
    for ep in RPCS:
        try:
            req = urllib.request.Request(ep, data=json.dumps({"jsonrpc": "2.0", "id": 1,
                "method": "eth_call", "params": [{"to": token, "data": data}, "latest"]}).encode(),
                headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=12) as r:
                res = json.loads(r.read()).get("result")
            if res and res != "0x":
                return int(res, 16)
        except Exception:
            pass
    return 0


def _approve_calldata(spender: str, amount: int) -> str:
    return "0x" + (keccak(text="approve(address,uint256)")[:4]
                   + encode(["address", "uint256"], [to_checksum_address(spender), amount])).hex()


def _wrap_calldata(asset: str, to: str, amount: int) -> str:
    return "0x" + (keccak(text="wrap(address,address,uint256)")[:4]
                   + encode(["address", "address", "uint256"],
                            [to_checksum_address(asset), to_checksum_address(to), amount])).hex()


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

    usdce = _bal(USDCE, wallet)
    pusd_before = _bal(PUSD, wallet)
    print("\n=== WRAP USDC.e -> pUSD (via relayer) ===\n")
    print(f"Deposit wallet : {wallet}")
    print(f"USDC.e balance : ${usdce/1e6:.4f}")
    print(f"pUSD before    : ${pusd_before/1e6:.4f}")
    if usdce <= 0:
        print("❌ No USDC.e to wrap — nothing to do."); return

    builder_config = BuilderConfig(
        local_builder_creds=BuilderApiKeyCreds(key=bkey, secret=bsecret, passphrase=bpass))
    client = RelayClient(relayer_url, chain_id, pk, builder_config)

    calls = [
        DepositWalletCall(target=to_checksum_address(USDCE), value="0",
                          data=_approve_calldata(ONRAMP, usdce)),
        DepositWalletCall(target=to_checksum_address(ONRAMP), value="0",
                          data=_wrap_calldata(USDCE, wallet, usdce)),
    ]
    print(f"\nWrapping ${usdce/1e6:.4f} USDC.e -> pUSD into the deposit wallet...")
    nonce = os.getenv("DEPOSIT_WALLET_NONCE") or str(_onchain_nonce(wallet))
    print(f"Using deposit-wallet nonce {nonce}")
    resp = client.execute_deposit_wallet_batch(
        calls=calls, wallet_address=wallet,
        nonce=nonce,
        deadline=os.getenv("DEPOSIT_WALLET_DEADLINE", str(int(time.time()) + 240)))
    print(f"  submitted: {resp}")
    try:
        print(f"  result: {resp.wait()}")
    except Exception as e:  # noqa: BLE001
        print(f"  wait() failed (check manually): {e}")

    time.sleep(3)
    print(f"\npUSD after     : ${_bal(PUSD, wallet)/1e6:.4f}")
    print("Next: spike_approve_deposit_wallet.py  then  spike_poly1271_probe.py")


if __name__ == "__main__":
    run()
