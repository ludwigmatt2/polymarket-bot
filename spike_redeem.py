"""
Claim (redeem) resolved Polymarket positions into pUSD, back into the deposit
wallet, gaslessly via the relayer. Routes per-position:
  - negativeRisk == True  -> NegRiskAdapter.redeemPositions(conditionId, [yes,no])  0xdbeccb23
  - else                  -> CTF.redeemPositions(pUSD, 0x0, conditionId, [1,2])      0x01b7037c
Both selectors verified in live bytecode. Neg-risk path needs the CTF
setApprovalForAll(NegRiskAdapter) we already set.

Only positions with data-api redeemable==True are touched (market resolved + you
hold the winning side). pUSD lands in the deposit wallet -> reinvest or withdraw.

SAFETY: dry-run by default (prints the calls). Set REDEEM_EXECUTE=1 to submit.

Run from the VPS (needs builder creds for the relayer):
  REDEEM_EXECUTE=1 venv/bin/python spike_redeem.py
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
PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
ZERO32 = b"\x00" * 32
MAINNET_RELAYER = "https://relayer-v2.polymarket.com/"
RPCS = ["https://polygon.llamarpc.com", "https://1rpc.io/matic",
        "https://polygon-bor-rpc.publicnode.com", "https://rpc.ankr.com/polygon"]


def _rpc(method, params):
    for ep in RPCS:
        try:
            req = urllib.request.Request(ep, data=json.dumps({"jsonrpc": "2.0", "id": 1,
                "method": method, "params": params}).encode(),
                headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=12) as r:
                o = json.loads(r.read())
                if o.get("result") is not None:
                    return o["result"]
        except Exception:
            pass
    return None


def _pusd(wallet):
    r = _rpc("eth_call", [{"to": PUSD, "data": "0x70a08231" + wallet.lower().replace("0x", "").zfill(64)}, "latest"])
    return int(r, 16) / 1e6 if r and r != "0x" else 0.0


def _nonce(wallet):
    r = _rpc("eth_call", [{"to": wallet, "data": "0xaffed0e0"}, "latest"])
    return int(r, 16) if r and r != "0x" else 0


def _positions(wallet):
    url = f"https://data-api.polymarket.com/positions?user={wallet}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=12) as r:
        return json.loads(r.read())


def _ctf_redeem_calldata(condition_id: str) -> str:
    sel = keccak(text="redeemPositions(address,bytes32,bytes32,uint256[])")[:4]
    args = encode(["address", "bytes32", "bytes32", "uint256[]"],
                  [to_checksum_address(PUSD), ZERO32,
                   bytes.fromhex(condition_id.replace("0x", "")), [1, 2]])
    return "0x" + (sel + args).hex()


def _negrisk_redeem_calldata(condition_id: str, amounts: list[int]) -> str:
    sel = keccak(text="redeemPositions(bytes32,uint256[])")[:4]
    args = encode(["bytes32", "uint256[]"],
                  [bytes.fromhex(condition_id.replace("0x", "")), amounts])
    return "0x" + (sel + args).hex()


def run() -> None:
    wallet = os.environ.get("DEPOSIT_WALLET", DEPOSIT_WALLET)
    execute = os.environ.get("REDEEM_EXECUTE") == "1"

    print("\n=== REDEEM RESOLVED POSITIONS -> pUSD ===\n")
    print(f"Deposit wallet : {wallet}")
    print(f"Mode           : {'EXECUTE' if execute else 'DRY-RUN (set REDEEM_EXECUTE=1 to submit)'}")
    print(f"pUSD before    : ${_pusd(wallet):.4f}")

    positions = _positions(wallet)
    redeemable = [p for p in positions if p.get("redeemable")]
    print(f"\nPositions: {len(positions)} total, {len(redeemable)} redeemable")
    if not redeemable:
        print("Nothing to redeem."); return

    # Group by conditionId so neg-risk amounts combine YES+NO holdings of one market.
    by_cond: dict[str, dict] = {}
    for p in redeemable:
        cond = p["conditionId"]
        g = by_cond.setdefault(cond, {"neg": bool(p.get("negativeRisk")), "amts": [0, 0], "info": []})
        idx = int(p.get("outcomeIndex", 0))  # 0=YES(set1), 1=NO(set2)
        g["amts"][idx] += int(round(float(p.get("size", 0)) * 1e6))
        g["info"].append(f"{p.get('outcome')} x{p.get('size')} — {str(p.get('title',''))[:40]}")

    from py_builder_relayer_client.models import DepositWalletCall
    calls = []
    for cond, g in by_cond.items():
        for line in g["info"]:
            print(f"  • {line}")
        if g["neg"]:
            data = _negrisk_redeem_calldata(cond, g["amts"])
            target = NEG_RISK_ADAPTER
            print(f"    -> NegRiskAdapter.redeemPositions({cond[:12]}…, {g['amts']})")
        else:
            data = _ctf_redeem_calldata(cond)
            target = CTF
            print(f"    -> CTF.redeemPositions(pUSD, 0x0, {cond[:12]}…, [1,2])")
        calls.append(DepositWalletCall(target=to_checksum_address(target), value="0", data=data))

    if not execute:
        print(f"\n[dry-run] would submit {len(calls)} redeem call(s) via the relayer.")
        return

    from py_builder_relayer_client.client import RelayClient
    from py_builder_signing_sdk.config import BuilderConfig, BuilderApiKeyCreds
    pk = os.getenv("POLYMARKET_PRIVATE_KEY") or os.getenv("PK")
    bk, bs, bp = os.getenv("BUILDER_API_KEY"), os.getenv("BUILDER_SECRET"), os.getenv("BUILDER_PASS_PHRASE")
    if not (pk and bk and bs and bp):
        print("❌ Missing builder creds for relayer."); return
    client = RelayClient(os.getenv("RELAYER_URL", MAINNET_RELAYER),
                         int(os.getenv("CHAIN_ID", "137")), pk,
                         BuilderConfig(local_builder_creds=BuilderApiKeyCreds(key=bk, secret=bs, passphrase=bp)))
    nonce = str(_nonce(wallet))
    print(f"\nSubmitting {len(calls)} redeem call(s), nonce {nonce}...")
    resp = client.execute_deposit_wallet_batch(
        calls=calls, wallet_address=wallet, nonce=nonce,
        deadline=str(int(time.time()) + 240))
    print(f"  submitted: {resp}")
    try:
        print(f"  result: {resp.wait()}")
    except Exception as e:  # noqa: BLE001
        print(f"  wait() failed (check manually): {e}")
    time.sleep(3)
    print(f"\npUSD after     : ${_pusd(wallet):.4f}")


if __name__ == "__main__":
    run()
