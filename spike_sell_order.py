"""
Sell the deposit wallet's outcome-token position back to pUSD (market SELL, FAK).
Sells the full position held in `asset` (from data-api), at the best bid.

Run from the Finland VPS:
  venv/bin/python spike_sell_order.py
Optional: SELL_TOKEN_ID=... SELL_SIZE=...  (default: full position from data-api)
"""

from __future__ import annotations

import json
import os
import time
import urllib.request

from dotenv import load_dotenv
from spike_eoa import _field

load_dotenv()

DEPOSIT_WALLET = "0xCeE18163EEb650177161a7174b760cf71D45bc8a"


def _position(wallet: str):
    url = f"https://data-api.polymarket.com/positions?user={wallet}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=12) as r:
        pos = json.loads(r.read())
    return pos[0] if pos else None


def run() -> None:
    from py_clob_client_v2 import ClobClient
    from py_clob_client_v2.clob_types import (
        ApiCreds, MarketOrderArgsV2, OrderType, PartialCreateOrderOptions,
    )
    from py_clob_client_v2.order_builder.constants import SELL

    pk = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
    if not pk:
        print("❌ POLYMARKET_PRIVATE_KEY not set"); return
    wallet = os.environ.get("DEPOSIT_WALLET", DEPOSIT_WALLET)

    token_id = os.environ.get("SELL_TOKEN_ID")
    size = os.environ.get("SELL_SIZE")
    if not token_id or not size:
        p = _position(wallet)
        if not p:
            print("No position to sell."); return
        token_id = token_id or p["asset"]
        size = size or str(p["size"])
        print(f"Position: {p['outcome']} x{p['size']} @ {p.get('curPrice')} — {str(p.get('title',''))[:50]}")
    size = float(size)

    print("\n=== MARKET SELL (deposit wallet, POLY_1271) ===\n")
    init = ClobClient(host="https://clob.polymarket.com", chain_id=137, key=pk,
                      signature_type=3, funder=wallet)
    c = init.create_or_derive_api_key()
    client = ClobClient(host="https://clob.polymarket.com", chain_id=137, key=pk,
                        creds=ApiCreds(c.api_key, c.api_secret, c.api_passphrase),
                        signature_type=3, funder=wallet)

    book = client.get_order_book(token_id)
    tick = str(_field(book, "tick_size") or "0.01")
    neg_risk = bool(_field(book, "neg_risk", False))
    bids = _field(book, "bids") or []
    best_bid = max((float(_field(b, "price")) for b in bids), default=0.0)
    print(f"token={token_id[:18]}…  best_bid={best_bid:.3f}  tick={tick}  "
          f"sell {size} (~${size*best_bid:.2f})")

    print("\nPosting market SELL (FAK)...")
    try:
        result = client.create_and_post_market_order(
            MarketOrderArgsV2(token_id=token_id, amount=size, side=SELL,
                              order_type=OrderType.FAK),
            options=PartialCreateOrderOptions(tick_size=tick, neg_risk=neg_risk),
            order_type=OrderType.FAK,
        )
        print(f"  RESPONSE: {result}")
        if result.get("success"):
            print(f"\n✅ SOLD — making={result.get('makingAmount')} taking={result.get('takingAmount')} "
                  f"status={result.get('status')}")
        else:
            print(f"  not filled: {result.get('errorMsg')}")
    except Exception as e:
        print(f"  EXCEPTION: {type(e).__name__}: {e}")
        if "min size" in str(e).lower() or "amount" in str(e).lower():
            print("  → likely under the $1 marketable minimum; position too small to market-sell.")

    time.sleep(2)
    p = _position(wallet)
    print(f"\nRemaining position: {p['size'] if p else 0}")


if __name__ == "__main__":
    run()
