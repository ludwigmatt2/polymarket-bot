"""
Phase 0 spike — Gnosis Safe path (your own Polymarket account, email login).

Usage:
  venv/bin/python spike_gnosis.py

Reads POLYMARKET_PRIVATE_KEY + POLYMARKET_PROXY_ADDRESS from .env (your existing keys).

What this tests:
  1. fetch_balance() works with gnosis-safe signature type
  2. build_order() + submit_order() succeed
  3. No nonce/proxy errors specific to Magic/email-login accounts

Decision after running:
  - All OK             → your own account works; admin live trading is unblocked
  - "proxy" error      → proxy_address is wrong or not set correctly
  - "nonce" / "sig"    → gnosis-safe signing has an issue; may need signature_type=1 (int)
  - Any other error    → note exact message for investigation
"""

from __future__ import annotations

import os
import time

from dotenv import load_dotenv

load_dotenv()


def _check_onchain_balance(address: str) -> float | None:
    """Read USDC.e balance on Polygon via public RPC — no credentials needed."""
    import json as _json
    USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
    # balanceOf(address) selector = 0x70a08231, padded to 32 bytes
    padded = address.lower().replace("0x", "").zfill(64)
    payload = _json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "eth_call",
        "params": [{"to": USDC_E, "data": "0x70a08231" + padded}, "latest"]
    }).encode()
    try:
        import requests as _req
        resp = _req.post(
            "https://polygon-bor-rpc.publicnode.com",
            json=_json.loads(payload),
            timeout=10,
        )
        result = resp.json()["result"]
        raw = int(result, 16)
        return raw / 1e6  # USDC.e has 6 decimals
    except Exception:
        return None


def run_spike() -> None:
    import pmxt

    pk    = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
    proxy = os.environ.get("POLYMARKET_PROXY_ADDRESS", "")

    if not pk or pk in ("0x...", ""):
        print("❌ POLYMARKET_PRIVATE_KEY not set in .env")
        return

    print("\n=== PHASE 0 SPIKE — GNOSIS-SAFE PATH ===\n")
    print(f"Key   : {pk[:6]}...{pk[-4:]}")
    print(f"Proxy : {proxy or '(none — will use EOA mode)'}")
    sig_type = "gnosis-safe" if proxy else "eoa"
    print(f"Mode  : signature_type={sig_type!r}\n")

    # ── 1. Connect ────────────────────────────────────────────────────────────
    print("[1/4] Connecting...")
    try:
        poly = pmxt.Polymarket(
            private_key=pk,
            proxy_address=proxy or None,
            signature_type=sig_type,
        )
        print("      OK")
    except Exception as e:
        _fail("connect", e)
        return

    # ── 2. Balance ────────────────────────────────────────────────────────────
    print("[2/4] Fetching balance...")

    # 2a. On-chain USDC.e balance via Polygon RPC (source of truth)
    # In EOA mode `proxy` is empty — derive the EOA address from the key so we
    # don't query the zero address (which always reads $0).
    check_addr = proxy
    if not check_addr:
        from eth_account import Account
        check_addr = Account.from_key(pk).address
    onchain = _check_onchain_balance(check_addr)
    if onchain is not None:
        print(f"      On-chain USDC.e (Polygon): ${onchain:.2f}")
    else:
        print("      On-chain check: failed (non-fatal)")

    # 2b. pmxt CLOB balance
    try:
        balances = poly.fetch_balance()
        print(f"      Raw pmxt balances: {balances}")
        usdc = 0.0
        for b in balances:
            currency = getattr(b, "currency", "")
            free = float(getattr(b, "free", 0) or 0)
            total = float(getattr(b, "total", 0) or 0)
            print(f"      pmxt {currency}: free={free} total={total}")
            if currency in ("USDC", "USDC.e"):
                usdc = free or total
    except Exception as e:
        _fail("fetch_balance", e)
        usdc = 0.0

    if onchain is None:
        print(f"\n      VERDICT balance: {'✅ $' + str(round(usdc,2)) if usdc > 0 else '❌ zero'}")
    elif onchain > 0 and usdc == 0:
        print("\n      ⚠️  Funds are on-chain but pmxt shows 0.")
        print("      This means the deposit is still being processed by Polymarket")
        print("      (they need to credit it to the CLOB). Usually takes 1-5 min.")
    elif onchain == 0 and usdc == 0:
        print("\n      ⏳ Deposit not yet on-chain — wait a few minutes and re-run.")
    else:
        print(f"\n      VERDICT balance: {'✅ $' + str(round(usdc,2)) if usdc > 0 else '❌ zero'}")

    if usdc <= 0:
        print("\nCan't test order placement without CLOB balance. Re-run once pmxt shows funds.")
        return

    # ── 3. Find a cheap weather market ───────────────────────────────────────
    print("\n[3/4] Finding a cheap weather market...")
    try:
        markets = poly.fetch_markets(active=True, limit=200)
        target = None
        for m in markets:
            title = getattr(m, "title", "") or ""
            if any(w in title.lower() for w in ["temperature", "weather", "celsius", "fahrenheit", "rain", "°"]):
                no_price = float(getattr(m.no, "price", 0) or 0)
                if 0.05 <= no_price <= 0.25:
                    target = m
                    print(f"      Market : {title[:70]}")
                    print(f"      NO price: {no_price:.3f}")
                    break
        if target is None:
            target = markets[0]
            print(f"      Fallback: {getattr(target, 'title', '?')[:70]}")
    except Exception as e:
        _fail("fetch_markets", e)
        return

    # ── 4. $1 order ───────────────────────────────────────────────────────────
    no_price = float(getattr(target.no, "price", 0.10) or 0.10)
    n_contracts = round(1.0 / no_price, 2)
    print(f"\n[4/4] Submitting $1 limit order: {n_contracts} contracts @ {no_price:.3f}...")

    order_id = None
    try:
        built = poly.build_order(
            market_id=target.market_id,
            outcome_id=target.no.market_id,
            side="buy",
            type="limit",
            amount=n_contracts,
            price=round(no_price, 4),
        )
        print("      build_order OK")
        order = poly.submit_order(built)
        order_id = str(getattr(order, "id", order))
        print(f"      submit_order OK — id={order_id}")
    except Exception as e:
        _fail("build/submit_order", e)
        _check_hints(e)
        return

    time.sleep(3)
    try:
        obj = poly.fetch_order(order_id)
        status = getattr(obj, "status", "unknown")
        filled = float(getattr(obj, "filled", 0) or 0)
        print(f"      Status={status}  filled={filled}")
    except Exception as e:
        print(f"      fetch_order (non-fatal): {e}")
        status, filled = "unknown", 0.0

    # ── Result ────────────────────────────────────────────────────────────────
    print("\n=== VERDICT ===")
    print(f"{'✅' if usdc > 0 else '❌'} Balance: ${round(usdc,2)}")
    print(f"{'✅' if order_id else '❌'} Order submitted: {order_id}")
    if order_id:
        print(f"   Status={status}  filled={filled}")
        print("\nGNOSIS-SAFE PATH: works — admin live trading unblocked")
        print(f"\n⚠️  Cancel the open order if you don't want it to fill:")
        print(f"   poly.cancel_order('{order_id}')")


def _fail(step: str, err: Exception) -> None:
    print(f"\n❌ FAILED at {step}: {type(err).__name__}: {err}")


def _check_hints(err: Exception) -> None:
    msg = str(err).lower()
    if "proxy" in msg or "funder" in msg:
        print("\n⚠️  PROXY ERROR: check POLYMARKET_PROXY_ADDRESS in .env")
        print("   Export it from polymarket.com → Settings → Export Key")
        print("   It's the Safe address shown there, not the signing key address")
    if "nonce" in msg or "signature" in msg or "sig" in msg:
        print("\n⚠️  SIGNATURE ERROR: try signature_type=1 (integer) instead of 'gnosis-safe'")
        print("   Magic/email-login accounts may use a different type code")
    if "approv" in msg or "allowance" in msg:
        print("\n⚠️  APPROVAL NEEDED — report this, we need to add ensure_approvals()")


if __name__ == "__main__":
    run_spike()
