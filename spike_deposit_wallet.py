"""
Read-only spike — derive this account's Polymarket V2 "deposit wallet" and
check whether it already exists on-chain.

WHY THIS EXISTS
---------------
Live order placement is blocked (see memory: order_placement_blocked). In late
April 2026 Polymarket migrated to CLOB V2, which no longer accepts an
email/Magic *proxy* wallet as an order `maker`. V2 demands a new primitive: a
deterministic CREATE2 / ERC-1271 "deposit wallet" used as maker+signer with
signatureType=3 (POLY_1271). The server message we hit:
    400 maker address not allowed, please use the deposit wallet flow

Before investing in the (hard) deposit-wallet order path, the first question is
purely informational: WHAT is our deposit-wallet address, and is it already
deployed + funded? The Polymarket UI trades fine for this account, so a deposit
wallet may already exist. This script answers that — READ ONLY.

WHAT IT DOES (no signing, no funds, no transactions)
----------------------------------------------------
  1. Derive the EOA from POLYMARKET_PRIVATE_KEY.
  2. Sanity-check the factory contract (BEACON()/implementation() == constants).
  3. Derive the deposit-wallet address via the factory's verified read function
     predictWalletAddress(bytes32(owner))  [selector 0x04f1d3c7]. This is the
     authoritative source — it equals the full CREATE2 derivation, with no
     init-code-hash risk (verified against py-builder-relayer-client).
  4. eth_getCode the derived address -> deployed yes/no.
  5. Read POL (gas) + USDC.e + native-USDC balances of the derived wallet.
  6. Cross-check via the factory's WalletDeployed event log (best-effort).
  7. Print a verdict + what it means for going live.

Usage:
  venv/bin/python spike_deposit_wallet.py

Constants verified on-chain (Polygon, chain 137) by research, June 2026:
  Factory        0x00000000000Fb5C9ADea0298D729A0CB3823Cc07  (DepositWalletFactory)
  Beacon         0x7A18EDfe055488A3128f01F563e5B479D92ffc3a
  Implementation 0x58CA52ebe0DadfdF531Cde7062e76746de4Db1eB
Sources: docs.polymarket.com/trading/deposit-wallets ;
         github.com/Polymarket/py-builder-relayer-client (builder/derive.py)
"""

from __future__ import annotations

import json
import os
import urllib.request

from dotenv import load_dotenv

load_dotenv("/Users/ludwigmatt/Projects/polymarket-bot/.env")

# ── Verified Polygon (137) constants ─────────────────────────────────────────
FACTORY = "0x00000000000Fb5C9ADea0298D729A0CB3823Cc07"
BEACON_EXPECTED = "0x7A18EDfe055488A3128f01F563e5B479D92ffc3a"
IMPL_EXPECTED = "0x58CA52ebe0DadfdF531Cde7062e76746de4Db1eB"

# Factory read selectors
SEL_PREDICT_WALLET = "0x04f1d3c7"  # predictWalletAddress(bytes32)
SEL_BEACON = "0x49493a4d"          # BEACON()
SEL_IMPLEMENTATION = "0x5c60da1b"  # implementation()

# WalletDeployed(address wallet, address owner, bytes32 id, address beacon)
# wallet, owner, id indexed. topic0 below (from official factory ABI).
TOPIC_WALLET_DEPLOYED = "0x7441de0ad639fe5d2bf1c22447715a0528b682385736bb40ae8dd92555eb8276"

# Collateral candidates on Polygon (USDC, 6 decimals)
TOKENS = {
    "USDC.e":      "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
    "USDC native": "0x3c499c542cEF5E3811e1192ce70d8cc03d5c3359",
}
SEL_BALANCE_OF = "0x70a08231"  # balanceOf(address)

# Public Polygon RPCs — tried in order, with fallback on error/403.
RPCS = [
    "https://polygon-rpc.com",
    "https://polygon.llamarpc.com",
    "https://1rpc.io/matic",
    "https://rpc.ankr.com/polygon",
    "https://polygon-bor-rpc.publicnode.com",
]


class Rpc:
    """Minimal JSON-RPC client with endpoint fallback (read-only methods)."""

    def __init__(self, endpoints: list[str]):
        self._endpoints = list(endpoints)
        self._active: str | None = None

    def call(self, method: str, params: list):
        last_err = None
        # Prefer the endpoint that last worked, then the rest.
        order = ([self._active] if self._active else []) + [
            e for e in self._endpoints if e != self._active
        ]
        for ep in order:
            try:
                body = json.dumps(
                    {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
                ).encode()
                req = urllib.request.Request(
                    ep,
                    data=body,
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": "Mozilla/5.0 (deposit-wallet-spike)",
                    },
                )
                with urllib.request.urlopen(req, timeout=15) as r:
                    out = json.loads(r.read())
                if "error" in out and out["error"]:
                    last_err = out["error"].get("message", out["error"])
                    continue  # try next endpoint
                self._active = ep
                return out.get("result")
            except Exception as e:  # noqa: BLE001 — best-effort across endpoints
                last_err = e
                continue
        raise RuntimeError(f"all RPCs failed for {method}: {last_err}")


def _addr_from_word(word: str) -> str:
    """Last 20 bytes of a 32-byte ABI word -> checksum-ish lowercase address."""
    return "0x" + word[-40:]


def _pad_addr(addr: str) -> str:
    """Address -> 32-byte left-padded hex (no 0x)."""
    return addr.lower().replace("0x", "").zfill(64)


def _read_addr(rpc: Rpc, to: str, selector: str, arg_word: str = "") -> str | None:
    res = rpc.call("eth_call", [{"to": to, "data": selector + arg_word}, "latest"])
    if not res or res == "0x" or int(res, 16) == 0:
        return None
    return _addr_from_word(res)


def _read_uint(rpc: Rpc, to: str, selector: str, arg_word: str = "") -> int:
    res = rpc.call("eth_call", [{"to": to, "data": selector + arg_word}, "latest"])
    if not res or res == "0x":
        return 0
    return int(res, 16)


def run() -> None:
    from eth_account import Account

    pk = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
    if not pk or pk in ("0x...", ""):
        print("❌ POLYMARKET_PRIVATE_KEY not set in .env")
        return
    eoa = Account.from_key(pk).address
    configured_proxy = (
        os.environ.get("POLYMARKET_FUNDER_ADDRESS")
        or os.environ.get("POLYMARKET_PROXY_ADDRESS")
        or ""
    )

    print("\n=== POLYMARKET V2 DEPOSIT-WALLET DERIVATION (read-only) ===\n")
    print(f"Owner EOA          : {eoa}")
    print(f"Configured proxy   : {configured_proxy or '(none)'}")

    rpc = Rpc(RPCS)

    # ── 1. Factory sanity ────────────────────────────────────────────────────
    print("\n[1/5] Verifying factory contract...")
    try:
        code = rpc.call("eth_getCode", [FACTORY, "latest"]) or "0x"
    except Exception as e:
        print(f"   ❌ RPC unreachable: {e}")
        return
    if code in ("0x", "0x0"):
        print(f"   ❌ No contract at factory {FACTORY} — wrong chain/address?")
        return
    beacon = _read_addr(rpc, FACTORY, SEL_BEACON)
    impl = _read_addr(rpc, FACTORY, SEL_IMPLEMENTATION)
    beacon_ok = beacon and beacon.lower() == BEACON_EXPECTED.lower()
    impl_ok = impl and impl.lower() == IMPL_EXPECTED.lower()
    print(f"   factory code      : {len(code)//2 - 1} bytes")
    print(f"   BEACON()          : {beacon}  {'✅' if beacon_ok else '⚠️  unexpected'}")
    print(f"   implementation()  : {impl}  {'✅' if impl_ok else '⚠️  unexpected'}")
    if not (beacon_ok and impl_ok):
        print("   ⚠️  Constants don't match — derivation below may be untrustworthy.")

    # ── 2. Derive deposit wallet (authoritative read fn) ─────────────────────
    print("\n[2/5] Deriving deposit wallet via predictWalletAddress(bytes32)...")
    wallet_id = _pad_addr(eoa)  # bytes32(owner)
    deposit_wallet = _read_addr(rpc, FACTORY, SEL_PREDICT_WALLET, wallet_id)
    if not deposit_wallet:
        print("   ❌ predictWalletAddress returned empty — cannot derive.")
        return
    print(f"   DEPOSIT WALLET    : {deposit_wallet}")
    if configured_proxy:
        same = deposit_wallet.lower() == configured_proxy.lower()
        print(f"   == configured proxy? {same}  "
              f"({'same address' if same else 'DIFFERENT — proxy is not the deposit wallet'})")

    # ── 3. Deployed on-chain? ────────────────────────────────────────────────
    print("\n[3/5] Checking whether the deposit wallet is deployed...")
    wcode = rpc.call("eth_getCode", [deposit_wallet, "latest"]) or "0x"
    deployed = wcode not in ("0x", "0x0")
    print(f"   code at wallet    : {len(wcode)//2 - 1} bytes -> "
          f"{'✅ DEPLOYED' if deployed else '❌ NOT deployed yet'}")

    # ── 4. Balances ──────────────────────────────────────────────────────────
    print("\n[4/5] Reading balances of the deposit wallet...")
    pol_wei = int(rpc.call("eth_getBalance", [deposit_wallet, "latest"]) or "0x0", 16)
    print(f"   POL (gas)         : {pol_wei / 1e18:.4f}")
    arg = _pad_addr(deposit_wallet)
    any_collateral = False
    for name, token in TOKENS.items():
        bal = _read_uint(rpc, token, SEL_BALANCE_OF, arg) / 1e6
        if bal > 0:
            any_collateral = True
        print(f"   {name:13}     : ${bal:,.2f}")

    # ── 5. WalletDeployed event cross-check (best-effort) ────────────────────
    print("\n[5/5] Cross-checking via WalletDeployed event log (best-effort)...")
    try:
        logs = rpc.call("eth_getLogs", [{
            "address": FACTORY,
            "fromBlock": "0x0",
            "toBlock": "latest",
            # topic0 = event sig; topic2 = owner (indexed) left-padded
            "topics": [TOPIC_WALLET_DEPLOYED, None, "0x" + _pad_addr(eoa)],
        }])
        if logs:
            logged_wallet = _addr_from_word(logs[-1]["topics"][1])
            match = logged_wallet.lower() == deposit_wallet.lower()
            print(f"   event wallet      : {logged_wallet}  "
                  f"{'✅ matches derivation' if match else '⚠️ MISMATCH'}")
        else:
            print("   no WalletDeployed log for this owner "
                  "(expected if undeployed, or RPC capped log range).")
    except Exception as e:  # noqa: BLE001
        print(f"   (log query unavailable — non-fatal: {e})")

    # ── Verdict ──────────────────────────────────────────────────────────────
    print("\n=== VERDICT ===")
    print(f"Deposit wallet : {deposit_wallet}")
    print(f"Deployed       : {'yes' if deployed else 'no'}")
    print(f"Has collateral : {'yes' if any_collateral else 'no'}")
    print(f"Has gas (POL)  : {'yes' if pol_wei > 0 else 'no'}")
    print()
    if deployed and any_collateral:
        print("→ Deposit wallet exists and is funded. The remaining blocker is purely")
        print("  the SDK L1/L2 auth binding (headers hard-code the EOA). Next step:")
        print("  prototype POLY_1271 ordering against THIS maker from the Finland VPS.")
    elif deployed:
        print("→ Deposit wallet exists but holds no USDC here. Funds may sit as a")
        print("  different V2 collateral, or in the proxy. Investigate before live.")
    else:
        print("→ Deposit wallet is NOT deployed. Going live this way would require")
        print("  deploying + funding it on-chain (writes + gas) — out of scope for a")
        print("  read-only check. Staying on paper remains the safe path.")
    print("\n(Read-only: no transactions sent, no keys exposed, no funds moved.)")


if __name__ == "__main__":
    run()
