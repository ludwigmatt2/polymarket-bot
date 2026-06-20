"""
Phase 0 spike — EOA path (fresh wallet created by the onboarding wizard).

Usage:
  # Step 1 — generate a throwaway EOA and get funding instructions:
  venv/bin/python spike_eoa.py --generate

  # Step 2 — fund the address shown (USDC.e + ~0.5 POL for gas), then:
  venv/bin/python spike_eoa.py --key 0x<private_key>

What this tests:
  1. get_balance_allowance() sees non-zero USDC collateral
  2. create_and_post_order() succeeds on a cheap weather market
  3. Order appears as open/unfilled in the book (or fills immediately)

Decision after running:
  - Balance > 0 AND order submitted → EOA path works, no ensure_approvals() needed
  - Balance = 0              → deposit not yet credited to CLOB; wait and retry
  - "allowance" in error     → add USDC approval step before first order
  - Any other error          → investigate and note exact message
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.parse
import urllib.request

from dotenv import load_dotenv

load_dotenv()


def generate_eoa() -> None:
    from eth_account import Account
    acct = Account.create()
    print("\n=== GENERATED THROWAWAY EOA ===")
    print(f"Address     : {acct.address}")
    print(f"Private key : {acct.key.hex()}")
    print()
    print("Next steps:")
    print("  1. Send ~$3 USDC.e to this address on Polygon")
    print("     (bridge from Ethereum or buy on exchange → withdraw to Polygon)")
    print("  2. Send ~0.5 POL to this address for gas")
    print("  3. Run: venv/bin/python spike_eoa.py --key", acct.key.hex())
    print()
    print("IMPORTANT: this key is shown once. Save it if you want to recover funds.")


def _find_cheap_weather_market() -> dict | None:
    """Use Gamma API to find a cheap active weather market (NO price 5–25¢)."""
    tick_map = {0.1: "0.1", 0.01: "0.01", 0.001: "0.001", 0.0001: "0.0001"}
    for term in ("temperature", "weather", "rain"):
        url = (
            "https://gamma-api.polymarket.com/markets"
            f"?active=true&limit=50&q={urllib.parse.quote(term)}"
        )
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                markets = json.loads(r.read())
        except Exception:
            continue
        items = markets if isinstance(markets, list) else markets.get("markets", [])
        for m in items:
            try:
                prices = json.loads(m.get("outcomePrices", "[]"))
                yes_p = float(prices[0]) if prices else 0.5
                no_p = 1.0 - yes_p
                token_ids = json.loads(m.get("clobTokenIds", "[]"))
                tick_raw = float(m.get("orderPriceMinTickSize", 0.01) or 0.01)
                if 0.05 <= no_p <= 0.25 and len(token_ids) >= 2:
                    return {
                        "title": m.get("question", m.get("slug", "?")),
                        "yes_price": yes_p,
                        "no_price": no_p,
                        "yes_token_id": token_ids[0],
                        "no_token_id": token_ids[1],
                        "tick_size": tick_map.get(tick_raw, "0.01"),
                    }
            except Exception:
                continue
    return None


def run_spike(pk: str) -> None:
    from py_clob_client_v2 import ClobClient
    from py_clob_client_v2.clob_types import (
        ApiCreds, AssetType, BalanceAllowanceParams,
        OrderArgsV2, PartialCreateOrderOptions,
    )
    from py_clob_client_v2.order_builder.constants import BUY

    print("\n=== PHASE 0 SPIKE — EOA PATH ===\n")

    # ── 1. Connect (L1 → derive L2 creds) ────────────────────────────────────
    print("[1/5] Connecting with EOA credentials (derives L2 API key)...")
    try:
        init_client = ClobClient(
            host="https://clob.polymarket.com",
            chain_id=137,
            key=pk,
            signature_type=0,  # EOA
        )
        creds = init_client.create_or_derive_api_key()
        print(f"      L2 key: {creds.api_key[:12]}...")
        client = ClobClient(
            host="https://clob.polymarket.com",
            chain_id=137,
            key=pk,
            creds=ApiCreds(
                api_key=creds.api_key,
                api_secret=creds.api_secret,
                api_passphrase=creds.api_passphrase,
            ),
            signature_type=0,
        )
        print("      OK")
    except Exception as e:
        _fail("connect", e)
        return

    # ── 2. Balance ────────────────────────────────────────────────────────────
    print("[2/5] Fetching CLOB balance...")
    balance = 0.0
    try:
        result = client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        print(f"      Raw response: {result}")
        balance = float(result.get("balance", result.get("allowance", "0")) or "0")
        verdict = f"✅ ${round(balance, 2)}" if balance > 0 else "❌ zero"
        print(f"\n      VERDICT balance: {verdict}")
    except Exception as e:
        _fail("get_balance_allowance", e)

    if balance <= 0:
        print("\nAborting: no CLOB balance. Deposit USDC.e to Polymarket first.")
        return

    # ── 3. Find a cheap active weather market ─────────────────────────────────
    print("\n[3/5] Finding a cheap active weather market via Gamma API...")
    market = _find_cheap_weather_market()
    if market is None:
        print("      ❌ No suitable market found (need NO price 5–25¢ with clobTokenIds)")
        return
    print(f"      Market     : {market['title'][:70]}")
    print(f"      YES={market['yes_price']:.3f}  NO={market['no_price']:.3f}")
    print(f"      NO token ID: {market['no_token_id'][:20]}...")

    # ── 4. Submit a $1 limit order ────────────────────────────────────────────
    no_price = market["no_price"]
    n_contracts = round(1.0 / no_price, 2)
    tick_size = market["tick_size"]
    print(f"\n[4/5] Building and submitting a $1 limit order (NO side)...")
    print(f"      {n_contracts} contracts @ {no_price:.3f}  tick={tick_size}")

    order_id = None
    try:
        result = client.create_and_post_order(
            OrderArgsV2(
                token_id=market["no_token_id"],
                price=no_price,
                size=n_contracts,
                side=BUY,
            ),
            options=PartialCreateOrderOptions(tick_size=tick_size, neg_risk=False),
        )
        order_id = result.get("orderID") or result.get("id") or str(result)
        print(f"      OK — order_id={order_id}")
    except Exception as e:
        _fail("create_and_post_order", e)
        _check_approval_hint(e)
        return

    # ── 5. Poll status ────────────────────────────────────────────────────────
    print("\n[5/5] Polling order status (3s)...")
    time.sleep(3)
    status, filled = "unknown", 0.0
    try:
        obj = client.get_order(order_id)
        status = str(obj.get("status", "unknown"))
        filled = float(obj.get("size_matched", obj.get("filled", 0)) or 0)
        print(f"      Status={status}  filled={filled}")
    except Exception as e:
        print(f"      get_order (non-fatal): {e}")

    # ── Result ────────────────────────────────────────────────────────────────
    print("\n=== VERDICT ===")
    print(f"{'✅' if balance > 0 else '❌'} Balance: ${round(balance, 2)} USDC")
    print(f"{'✅' if order_id else '❌'} Order submitted: {order_id}")
    if order_id:
        print(f"   Status={status}  filled={filled}")
        if status.upper() in ("OPEN", "LIVE"):
            print("   → Limit order sitting in book (normal — may fill later)")
        elif filled > 0:
            print("   → Filled immediately!")
        print("\nEOA PATH: works — no ensure_approvals() needed in live_trader.py")
        print(f"\n⚠️  Cancel the open order via Polymarket UI or note the ID:")
        print(f"   order_id = {order_id!r}")


def _fail(step: str, err: Exception) -> None:
    print(f"\n❌ FAILED at {step}: {type(err).__name__}: {err}")


def _check_approval_hint(err: Exception) -> None:
    msg = str(err).lower()
    if any(k in msg for k in ("approv", "allowance", "permit", "erc20")):
        print("\n⚠️  APPROVAL NEEDED: USDC approval may be required for the CLOB contract.")
        print("   Report exact error above so we can investigate.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--generate", action="store_true", help="Generate a new throwaway EOA")
    parser.add_argument("--key", help="Private key of funded EOA to test")
    args = parser.parse_args()

    if args.generate:
        generate_eoa()
    elif args.key:
        run_spike(args.key)
    else:
        parser.print_help()
