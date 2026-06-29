"""
Phase 0 probe — capture the EXACT server error for a POLY_1271 (deposit-wallet)
order. MUST run from the Finland VPS (geoblock), where /opt/polymarket-bot/.env
holds POLYMARKET_PRIVATE_KEY.

WHAT IT DOES
  1. Auth as signature_type=3 (POLY_1271), funder = our derived deposit wallet.
     create_or_derive_api_key binds the L2 key to the EOA — that's the crux.
  2. Build + sign a POLY_1271 limit BUY, post_only, far below market (cannot fill).
  3. POST it and print the server response/exception VERBATIM, then classify.

This is a doomed order by design — it tells us WHICH wall the server enforces:
  - "maker address not allowed / deposit wallet flow" -> V2 maker rejection
  - "order signer ... has to be ... the API KEY"       -> #75 EOA-binding (expected)
  - "balance / allowance / not deployed / funds"        -> got PAST auth (notable)

OUTWARD ACTION: posts to Polymarket /order. post_only + far-from-market => no
fill; expected to be rejected. Run only when intending to live-probe.

Run on the VPS:
  cd /opt/polymarket-bot && venv/bin/python spike_poly1271_probe.py
Optional override: DEPOSIT_WALLET=0x...  (defaults to our derived wallet)
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

# spike_eoa lives in the repo root; reuse its live-token finder + fail helper.
from spike_eoa import _find_tradeable_token, _fail

load_dotenv()

# Our account's derived V2 deposit wallet (spike_deposit_wallet.py, verified).
DEFAULT_DEPOSIT_WALLET = "0xcee18163eeb650177161a7174b760cf71d45bc8a"


def _classify(msg: str) -> None:
    m = msg.lower()
    print("\n=== VERDICT ===")
    if "maker address not allowed" in m or "deposit wallet flow" in m:
        print("V2 MAKER WALL: server rejects the deposit-wallet maker outright.")
    elif "signer" in m and "api key" in m:
        print("EOA-BINDING BUG (#75): order.signer = deposit wallet, but the API key")
        print("is bound to the EOA. Confirms the auth-binding wall — needs an SDK patch")
        print("that binds the key to the deposit wallet (ERC-1271 ClobAuth).")
    elif any(k in m for k in ("balance", "allowance", "not deployed", "funds", "approve")):
        print("PAST THE AUTH WALL — rejected on funding/deployment, not signer binding.")
        print("→ Auth path may be OK once the wallet is deployed + funded + approved.")
    else:
        print("Unclassified — record verbatim:")
        print(f"  {msg}")


def run() -> None:
    from py_clob_client_v2 import ClobClient
    from py_clob_client_v2.clob_types import (
        ApiCreds, OrderArgsV2, PartialCreateOrderOptions,
    )
    from py_clob_client_v2.order_builder.constants import BUY

    pk = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
    if not pk or pk in ("0x...", ""):
        print("❌ POLYMARKET_PRIVATE_KEY not set")
        return
    deposit_wallet = os.environ.get("DEPOSIT_WALLET", DEFAULT_DEPOSIT_WALLET)

    print("\n=== PHASE 0 — POLY_1271 ORDER PROBE (deposit-wallet maker) ===\n")
    print(f"Funder (deposit wallet): {deposit_wallet}")
    print("signature_type         : 3 (POLY_1271)\n")

    # ── 1. Auth (binds L2 key to the EOA) ────────────────────────────────────
    print("[1/3] Authenticating (sig_type=3, funder=deposit wallet)...")
    try:
        init = ClobClient(
            host="https://clob.polymarket.com", chain_id=137, key=pk,
            signature_type=3, funder=deposit_wallet,
        )
        creds = init.create_or_derive_api_key()
        client = ClobClient(
            host="https://clob.polymarket.com", chain_id=137, key=pk,
            creds=ApiCreds(creds.api_key, creds.api_secret, creds.api_passphrase),
            signature_type=3, funder=deposit_wallet,
        )
        print(f"      L2 key {creds.api_key[:12]}… (bound to the EOA, not the wallet)")
    except Exception as e:
        _fail("authenticate", e)
        _classify(str(e))
        return

    # ── 2. Live token ────────────────────────────────────────────────────────
    print("\n[2/3] Finding a live token...")
    market = _find_tradeable_token(client)
    if market is None:
        print("      ❌ no order-accepting token found")
        return
    ref = market["price"]
    tick = float(market["tick_size"])
    bid = max(tick, round(round(ref * 0.25 / tick) * tick, 6))  # 75% below market
    min_sz = float(market.get("min_order_size") or 5)
    n = round(max(1.0 / bid, min_sz), 2)
    print(f"      token={market['token_id'][:18]}…  market≈{ref:.3f}  bid={bid:.4f}  size={n}")

    # ── 3. Build → post POLY_1271 order, capture the exact error ─────────────
    print("\n[3/3] Posting POLY_1271 post_only BUY (won't fill)...")
    try:
        result = client.create_and_post_order(
            OrderArgsV2(token_id=market["token_id"], price=bid, size=n, side=BUY),
            options=PartialCreateOrderOptions(
                tick_size=market["tick_size"], neg_risk=market["neg_risk"]
            ),
            post_only=True,
        )
        print(f"      RESPONSE: {result}")
        oid = result.get("orderID") or result.get("id")
        if oid:
            print(f"\n🎉 UNEXPECTED: order accepted id={oid} — deposit-wallet path WORKS.")
            print("   Cancel it in the UI and re-evaluate going live.")
        else:
            _classify(str(result))
    except Exception as e:
        print(f"      EXCEPTION: {type(e).__name__}: {e}")
        _classify(str(e))


if __name__ == "__main__":
    run()
