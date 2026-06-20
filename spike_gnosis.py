"""
Phase 0 spike — POLY_PROXY path (your own Polymarket account, email/Google login).

Usage:
  venv/bin/python spike_gnosis.py

Reads POLYMARKET_PRIVATE_KEY + POLYMARKET_PROXY_ADDRESS (or POLYMARKET_FUNDER_ADDRESS)
from .env (your existing keys).

What this tests:
  1. get_balance_allowance() works with POLY_PROXY signature type (1)
  2. create_and_post_order() succeeds with a funder address
  3. No proxy/nonce errors specific to email/Google-login accounts

Decision after running:
  - All OK          → your own account works; admin live trading is unblocked
  - "proxy" error   → funder_address is wrong or not set correctly
  - Balance = 0     → funds may still be processing (Polymarket credits CLOB after deposit)
  - Any other error → note exact message for investigation

Note: "POLY_PROXY" (signature_type=1) is what pmxt used to call "gnosis-safe".
It is NOT the same as a Gnosis Safe multisig. It's the standard email/Google
Polymarket login flow where Polymarket holds a proxy wallet on your behalf.
"""

from __future__ import annotations

import os
import time

from dotenv import load_dotenv
from spike_eoa import _fail, _find_cheap_weather_market

load_dotenv()


def _check_onchain_balance(address: str) -> float | None:
    """Read USDC.e balance on Polygon via public RPC — no credentials needed."""
    import requests as _req
    USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
    padded = address.lower().replace("0x", "").zfill(64)
    try:
        resp = _req.post(
            "https://polygon-bor-rpc.publicnode.com",
            json={
                "jsonrpc": "2.0", "id": 1, "method": "eth_call",
                "params": [{"to": USDC_E, "data": "0x70a08231" + padded}, "latest"],
            },
            timeout=10,
        )
        return int(resp.json()["result"], 16) / 1e6
    except Exception:
        return None


def run_spike() -> None:
    from py_clob_client_v2 import ClobClient
    from py_clob_client_v2.clob_types import (
        ApiCreds, AssetType, BalanceAllowanceParams,
        OrderArgsV2, OrderPayload, PartialCreateOrderOptions,
    )
    from py_clob_client_v2.order_builder.constants import BUY

    pk = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
    funder = (
        os.environ.get("POLYMARKET_FUNDER_ADDRESS")
        or os.environ.get("POLYMARKET_PROXY_ADDRESS")
        or ""
    )

    if not pk or pk in ("0x...", ""):
        print("❌ POLYMARKET_PRIVATE_KEY not set in .env")
        return

    sig_type = 1 if funder else 0  # POLY_PROXY vs EOA
    mode_label = "POLY_PROXY / signature_type=1" if funder else "EOA / signature_type=0 (no funder set)"

    print("\n=== PHASE 0 SPIKE — POLY_PROXY PATH ===\n")
    print(f"Key    : {pk[:6]}...{pk[-4:]}")
    print(f"Funder : {funder or '(none — falling back to EOA mode)'}")
    print(f"Mode   : {mode_label}\n")

    # ── 1. Connect ────────────────────────────────────────────────────────────
    print("[1/5] Connecting and deriving L2 API key...")
    try:
        init_client = ClobClient(
            host="https://clob.polymarket.com",
            chain_id=137,
            key=pk,
            signature_type=sig_type,
            funder=funder or None,
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
            signature_type=sig_type,
            funder=funder or None,
        )
        print("      OK")
    except Exception as e:
        _fail("connect", e)
        return

    # ── 2. Balance ────────────────────────────────────────────────────────────
    print("[2/5] Fetching balance...")

    check_addr = funder or ""
    if not check_addr:
        from eth_account import Account
        check_addr = Account.from_key(pk).address
    onchain = _check_onchain_balance(check_addr)
    if onchain is not None:
        print(f"      On-chain USDC.e (Polygon): ${onchain:.2f}")

    balance = 0.0
    try:
        result = client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        print(f"      CLOB response: {result}")
        # CLOB returns USDC collateral in 6-decimal base units ("7330000" = $7.33)
        balance = float(result.get("balance", "0") or "0") / 1_000_000
    except Exception as e:
        _fail("get_balance_allowance", e)

    if onchain is not None and onchain > 0 and balance == 0:
        print("\n      ⚠️  Funds on-chain but CLOB shows 0.")
        print("      Polymarket may still be crediting the deposit (wait 1–5 min).")
    elif onchain is not None and onchain == 0 and balance == 0:
        print("\n      ⏳ Deposit not yet on-chain — wait a few minutes and re-run.")
    else:
        verdict = f"✅ ${round(balance, 2)}" if balance > 0 else "❌ zero"
        print(f"\n      VERDICT balance: {verdict}")

    if balance <= 0:
        print("\nCan't test order placement without CLOB balance. Re-run once balance shows up.")
        return

    # ── 3. Find a cheap weather market ───────────────────────────────────────
    print("\n[3/5] Finding a cheap weather market via Gamma API...")
    market = _find_cheap_weather_market()
    if market is None:
        print("      ❌ No suitable market found (need NO price 5–25¢ with clobTokenIds)")
        return
    print(f"      Market     : {market['title'][:70]}")
    print(f"      YES={market['yes_price']:.3f}  NO={market['no_price']:.3f}")
    print(f"      NO token ID: {market['no_token_id'][:20]}...")

    # ── 4. Resting NO bid, well below market — validates the post path without
    #       taking a position. post_only makes it maker-only (rejected, never
    #       filled, if it would ever cross the book). ──────────────────────────
    no_price = market["no_price"]
    tick = float(market["tick_size"])
    bid = max(tick, round(round(no_price * 0.25 / tick) * tick, 6))  # 75% below market
    n_contracts = round(max(1.0 / bid, 5.0), 2)
    print(f"\n[4/5] Posting a resting NO bid (~${bid * n_contracts:.2f}, won't fill)...")
    print(f"      {n_contracts} @ {bid:.4f}   (market NO={no_price:.3f}, tick={market['tick_size']})")

    order_id = None
    try:
        result = client.create_and_post_order(
            OrderArgsV2(
                token_id=market["no_token_id"],
                price=bid,
                size=n_contracts,
                side=BUY,
            ),
            options=PartialCreateOrderOptions(tick_size=market["tick_size"], neg_risk=False),
            post_only=True,
        )
        order_id = result.get("orderID") or result.get("id") or str(result)
        print(f"      OK — posted id={order_id}")
    except Exception as e:
        _fail("create_and_post_order", e)
        _check_hints(e)
        return

    # ── 5. Cancel the test order ────────────────────────────────────────────────
    print("\n[5/5] Cancelling the test order...")
    time.sleep(2)  # let the order register before cancelling
    cancelled = False
    try:
        resp = client.cancel_order(OrderPayload(orderID=order_id))
        cancelled = True
        print(f"      Cancel response: {resp}")
    except Exception as e:
        print(f"      ⚠️ cancel failed: {e}")
        print(f"      Cancel manually in the Polymarket UI → order_id={order_id!r}")

    # ── Result ────────────────────────────────────────────────────────────────
    print("\n=== VERDICT ===")
    print(f"{'✅' if balance > 0 else '❌'} Balance: ${round(balance, 2)}")
    print(f"{'✅' if order_id else '❌'} Order posted:    {order_id}")
    print(f"{'✅' if cancelled else '⚠️ '} Order cancelled: {cancelled}")
    if order_id and cancelled:
        print("\nPOLY_PROXY PATH: create → post → cancel all work — live trading unblocked.")
    elif order_id:
        print("\nPOLY_PROXY PATH: order posts, but cancel failed — cancel the ID above manually.")


def _check_hints(err: Exception) -> None:
    msg = str(err).lower()
    if "proxy" in msg or "funder" in msg:
        print("\n⚠️  PROXY ERROR: check POLYMARKET_FUNDER_ADDRESS / POLYMARKET_PROXY_ADDRESS in .env")
        print("   It's the Safe address shown at polymarket.com → Settings → Export Key")
    if "nonce" in msg or "signature" in msg or "sig" in msg:
        print("\n⚠️  SIGNATURE ERROR: verify signature_type=1 (POLY_PROXY) is correct for your account")
        print("   EOA accounts use signature_type=0")
    if "approv" in msg or "allowance" in msg:
        print("\n⚠️  APPROVAL NEEDED — report this exact error for investigation")


if __name__ == "__main__":
    run_spike()
