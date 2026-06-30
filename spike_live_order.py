"""
Place a REAL ~$1 marketable order from the deposit wallet and confirm a fill.
Also cancels the resting post_only test order first (env CANCEL_ORDER_ID).

This buys ~$1 of a liquid YES/NO token at the best ask (FAK: fills immediately at
or inside the limit, kills any remainder). It TAKES A REAL POSITION (~$1).

Run from the Finland VPS:
  CANCEL_ORDER_ID=0x... venv/bin/python spike_live_order.py
"""

from __future__ import annotations

import os
import time

from dotenv import load_dotenv
from spike_eoa import _field

load_dotenv()

DEPOSIT_WALLET = "0xCeE18163EEb650177161a7174b760cf71D45bc8a"
SPEND_USD = float(os.getenv("SPEND_USD", "1.0"))


def _best_ask(client, token_id: str):
    """(price, size) of the best ask for token_id, or (None, None)."""
    book = client.get_order_book(token_id)
    asks = _field(book, "asks") or []
    best = None
    for a in asks:  # CLOB returns asks ascending or descending — pick the min price
        p = float(_field(a, "price"))
        s = float(_field(a, "size"))
        if best is None or p < best[0]:
            best = (p, s)
    return best if best else (None, None)


def _find_liquid_token(client):
    """A token with a real ask between 0.05 and 0.95 and enough size for ~$1."""
    for getter in ("get_sampling_simplified_markets", "get_simplified_markets"):
        try:
            resp = getattr(client, getter)()
        except Exception:
            continue
        rows = resp.get("data", []) if isinstance(resp, dict) else (resp or [])
        for mk in rows:
            for t in (_field(mk, "tokens", []) or []):
                tid = _field(t, "token_id")
                if not tid:
                    continue
                ask_p, ask_s = _best_ask(client, tid)
                if ask_p and 0.05 <= ask_p <= 0.95 and ask_s and ask_s * ask_p >= SPEND_USD:
                    book = client.get_order_book(tid)
                    return {
                        "token_id": tid, "ask": ask_p,
                        "tick_size": str(_field(book, "tick_size") or "0.01"),
                        "neg_risk": bool(_field(book, "neg_risk", False)),
                        "condition_id": _field(mk, "condition_id", "?"),
                        "outcome": _field(t, "outcome", "?"),
                    }
    return None


def run() -> None:
    from py_clob_client_v2 import ClobClient
    from py_clob_client_v2.clob_types import (
        ApiCreds, OrderArgsV2, OrderType, PartialCreateOrderOptions, OrderPayload,
    )
    from py_clob_client_v2.order_builder.constants import BUY

    pk = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
    if not pk:
        print("❌ POLYMARKET_PRIVATE_KEY not set"); return
    wallet = os.environ.get("DEPOSIT_WALLET", DEPOSIT_WALLET)

    print("\n=== LIVE ~$1 ORDER (deposit wallet, POLY_1271) ===\n")
    init = ClobClient(host="https://clob.polymarket.com", chain_id=137, key=pk,
                      signature_type=3, funder=wallet)
    c = init.create_or_derive_api_key()
    client = ClobClient(host="https://clob.polymarket.com", chain_id=137, key=pk,
                        creds=ApiCreds(c.api_key, c.api_secret, c.api_passphrase),
                        signature_type=3, funder=wallet)

    # Cancel the resting test order, if provided
    cancel_id = os.environ.get("CANCEL_ORDER_ID")
    if cancel_id:
        try:
            resp = client.cancel_order(OrderPayload(orderID=cancel_id))
            print(f"[cancel] {cancel_id[:14]}… -> {resp}")
        except Exception as e:
            print(f"[cancel] failed (non-fatal): {e}")

    print("\n[1/2] Finding a liquid token...")
    mk = _find_liquid_token(client)
    if not mk:
        print("      ❌ no liquid token found for a $1 fill"); return
    ask = mk["ask"]
    # Cross the spread to guarantee a fill: bid at the ask (FAK fills at/inside).
    size = round(SPEND_USD / ask, 2)
    print(f"      token={mk['token_id'][:18]}…  outcome={mk['outcome']}  ask={ask:.3f}  "
          f"tick={mk['tick_size']}  neg_risk={mk['neg_risk']}")
    print(f"      buying {size} @ {ask:.3f}  (~${size*ask:.2f}), FAK")

    print("\n[2/2] Posting marketable BUY (FAK)...")
    result = client.create_and_post_order(
        OrderArgsV2(token_id=mk["token_id"], price=ask, size=size, side=BUY),
        options=PartialCreateOrderOptions(tick_size=mk["tick_size"], neg_risk=mk["neg_risk"]),
        order_type=OrderType.FAK,
    )
    print(f"      RESPONSE: {result}")
    oid = result.get("orderID") or result.get("id")
    if not oid:
        print("      ❌ no order id returned"); return

    time.sleep(3)
    try:
        obj = client.get_order(oid)
        matched = float(obj.get("size_matched", 0) or 0) / 1e6
        print(f"\n=== RESULT ===")
        print(f"order_id : {oid}")
        print(f"status   : {obj.get('status')}")
        print(f"filled   : {matched} contracts (~${matched*ask:.2f})")
        if matched > 0:
            print("\n✅✅ REAL FILL — live trading via the deposit wallet works end-to-end.")
        else:
            print("\n(order placed but unfilled — FAK killed it; book may have moved)")
    except Exception as e:
        print(f"get_order failed (non-fatal): {e}")


if __name__ == "__main__":
    run()
