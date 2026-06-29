"""
Phase 0 spike — order placement via the ORIGINAL py-clob-client (v1, 0.34.x).

Why: py-clob-client-v2 (1.0.1) rejects order placement for Magic/email proxy
wallets with 400 "maker address not allowed, please use the deposit wallet flow"
(open upstream bug, gateway-level — signature_type changes don't help). v2 reads
(balance/markets) work fine; only order POST is broken. v1 is the mature client
most Polymarket bots use for exactly this POLY_PROXY + funder flow. This spike
checks whether v1 can post (and cancel) a resting order for this account.

Run on the Finland VPS (permitted region):
  venv/bin/pip install py-clob-client==0.34.6     # coexists with v2
  venv/bin/python spike_v1.py

Reads POLYMARKET_PRIVATE_KEY + POLYMARKET_FUNDER_ADDRESS (or _PROXY_) from .env.
Optional POLYMARKET_SIG_TYPE to override the signature type (default 1=POLY_PROXY).
"""

from __future__ import annotations

import os
import time

from dotenv import load_dotenv

load_dotenv()


def _fail(step: str, err: Exception) -> None:
    print(f"\n❌ FAILED at {step}: {type(err).__name__}: {err}")


def _field(obj, name, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _find_tradeable_token(client) -> dict | None:
    """Pick an order-accepting token via sampling markets + tick/neg-risk lookups."""
    rows = []
    for getter in ("get_sampling_simplified_markets", "get_simplified_markets"):
        try:
            resp = getattr(client, getter)()
        except Exception:
            continue
        rows = resp.get("data", []) if isinstance(resp, dict) else (resp or [])
        if rows:
            break
    for mk in rows:
        for t in _field(mk, "tokens", []) or []:
            tid = _field(t, "token_id")
            try:
                price = float(_field(t, "price") or 0)
            except (TypeError, ValueError):
                continue
            if not tid or not (0.05 <= price <= 0.95):
                continue
            try:
                tick = client.get_tick_size(tid)
                neg = client.get_neg_risk(tid)
            except Exception:
                continue
            return {
                "token_id": tid, "price": price,
                "condition_id": _field(mk, "condition_id", "?"),
                "tick_size": str(tick), "neg_risk": bool(neg),
            }
    return None


def run_spike() -> None:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import (
        ApiCreds, AssetType, BalanceAllowanceParams,
        OrderArgs, OrderType, PartialCreateOrderOptions,
    )
    from py_clob_client.order_builder.constants import BUY

    pk = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
    funder = (
        os.environ.get("POLYMARKET_FUNDER_ADDRESS")
        or os.environ.get("POLYMARKET_PROXY_ADDRESS")
        or ""
    )
    if not pk or pk in ("0x...", ""):
        print("❌ POLYMARKET_PRIVATE_KEY not set in .env")
        return

    sig_type = int(os.environ.get("POLYMARKET_SIG_TYPE") or (1 if funder else 0))

    print("\n=== PHASE 0 SPIKE — py-clob-client v1 (order placement) ===\n")
    print(f"Key     : {pk[:6]}...{pk[-4:]}")
    print(f"Funder  : {funder or '(none — EOA mode)'}")
    print(f"SigType : {sig_type}\n")

    # ── 1. Connect + L2 creds ────────────────────────────────────────────────
    print("[1/5] Connecting and deriving L2 API creds...")
    try:
        client = ClobClient(
            host="https://clob.polymarket.com",
            chain_id=137,
            key=pk,
            signature_type=sig_type,
            funder=funder or None,
        )
        creds: "ApiCreds" = client.create_or_derive_api_creds()
        client.set_api_creds(creds)
        print(f"      L2 key: {str(creds.api_key)[:12]}...  OK")
    except Exception as e:
        _fail("connect", e)
        return

    # ── 2. Balance ───────────────────────────────────────────────────────────
    print("[2/5] Fetching balance...")
    balance = 0.0
    try:
        result = client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        raw = _field(result, "balance", "0") or "0"
        balance = float(raw) / 1_000_000   # CLOB returns 6-decimal base units
        print(f"      VERDICT balance: {'✅ $' + str(round(balance, 2)) if balance > 0 else '❌ zero'}")
    except Exception as e:
        _fail("get_balance_allowance", e)

    # ── 3. Find a market ───────────────────────────────────────────────────────
    print("\n[3/5] Finding an order-accepting market via the CLOB API...")
    market = _find_tradeable_token(client)
    if market is None:
        print("      ❌ No order-accepting token found.")
        return
    print(f"      condition={market['condition_id'][:12]}…  price={market['price']:.3f}  "
          f"tick={market['tick_size']}  neg_risk={market['neg_risk']}")

    # ── 4. Resting bid 75% below market (post_only → never fills) ───────────────
    ref = market["price"]
    tick = float(market["tick_size"])
    bid = max(tick, round(round(ref * 0.25 / tick) * tick, 6))
    n_contracts = round(max(1.0 / bid, 5.0), 2)
    print(f"\n[4/5] Posting a resting BUY bid (~${bid * n_contracts:.2f}, won't fill)...")
    print(f"      {n_contracts} @ {bid:.4f}   (market={ref:.3f}, tick={market['tick_size']})")

    order_id = None
    try:
        signed = client.create_order(
            OrderArgs(token_id=market["token_id"], price=bid, size=n_contracts, side=BUY),
            PartialCreateOrderOptions(tick_size=market["tick_size"], neg_risk=market["neg_risk"]),
        )
        resp = client.post_order(signed, OrderType.GTC, post_only=True)
        order_id = _field(resp, "orderID") or _field(resp, "orderId") or _field(resp, "id")
        print(f"      post_order response: {resp}")
        if not order_id:
            print("      ⚠️ no order id in response — treating as failure")
    except Exception as e:
        _fail("post_order", e)
        return

    # ── 5. Cancel ──────────────────────────────────────────────────────────────
    print("\n[5/5] Cancelling the test order...")
    time.sleep(2)
    cancelled = False
    try:
        cresp = client.cancel(order_id)
        cancelled = True
        print(f"      cancel response: {cresp}")
    except Exception as e:
        print(f"      ⚠️ cancel failed: {e}  — cancel manually: order_id={order_id!r}")

    print("\n=== VERDICT ===")
    print(f"{'✅' if balance > 0 else '❌'} Balance: ${round(balance, 2)}")
    print(f"{'✅' if order_id else '❌'} Order posted:    {order_id}")
    print(f"{'✅' if cancelled else '⚠️ '} Order cancelled: {cancelled}")
    if order_id and cancelled:
        print("\nv1 PATH WORKS: create → post → cancel all succeed. "
              "Migrate the live order path to py-clob-client v1.")
    elif order_id:
        print("\nv1 posts orders but cancel failed — cancel the ID above manually.")


if __name__ == "__main__":
    run_spike()
