"""
Test the SIMPLE path we skipped: can a plain EOA (signature_type=0, maker=signer=
EOA) place a V2 order? If yes, go-live needs no deposit wallet and no SDK patch —
just fund an EOA and trade. The SDK already binds the API key to the EOA
correctly, so the #75 auth-binding wall does NOT apply here.

This posts a doomed post_only order (far below market, can't fill) with the EOA
as maker, and reads the EXACT rejection — no funds needed:
  - "not enough balance" / "allowance" / accepted -> EOA ordering WORKS in V2.
    Fund the EOA (or move funds out of the Magic proxy) and we're live, today.
  - "maker address not allowed" / "deposit wallet flow" -> EOA ordering IS blocked
    in V2; the deposit-wallet server gap is the only path (stay paper).

Run from the Finland VPS:
  cd /opt/polymarket-bot && venv/bin/python spike_eoa_probe.py
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

from spike_eoa import _find_tradeable_token, _fail

load_dotenv()


def _classify(msg: str) -> None:
    m = msg.lower()
    print("\n=== VERDICT ===")
    if "maker address not allowed" in m or "deposit wallet flow" in m:
        print("EOA ordering BLOCKED in V2 — maker rejected. Deposit-wallet path is the")
        print("only option, and its server auth is broken. Stay paper.")
    elif any(k in m for k in ("balance", "allowance", "not enough", "funds", "insufficient")):
        print("✅ EOA PATH WORKS — rejected only for lack of funds, NOT for the maker/auth.")
        print("→ Fund the EOA (or withdraw from the Magic proxy to it), trade signature_type=0.")
        print("  No deposit wallet, no SDK patch, no waiting.")
    elif "minimum" in m or "tick" in m or "price" in m:
        print("✅ EOA PATH WORKS — order reached book validation (size/price), past maker/auth.")
        print("→ Same conclusion: fund the EOA and trade signature_type=0.")
    else:
        print("Unclassified — record verbatim:")
        print(f"  {msg}")


def run() -> None:
    from py_clob_client_v2 import ClobClient
    from py_clob_client_v2.clob_types import ApiCreds, OrderArgsV2, PartialCreateOrderOptions
    from py_clob_client_v2.order_builder.constants import BUY
    from eth_account import Account

    pk = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
    if not pk or pk in ("0x...", ""):
        print("❌ POLYMARKET_PRIVATE_KEY not set"); return
    eoa = Account.from_key(pk).address

    print("\n=== EOA-PATH PROBE (signature_type=0, maker=signer=EOA) ===\n")
    print(f"EOA (maker+signer): {eoa}\n")

    print("[1/3] Authenticating as a plain EOA (no funder)...")
    try:
        init = ClobClient(host="https://clob.polymarket.com", chain_id=137, key=pk, signature_type=0)
        creds = init.create_or_derive_api_key()
        client = ClobClient(
            host="https://clob.polymarket.com", chain_id=137, key=pk,
            creds=ApiCreds(creds.api_key, creds.api_secret, creds.api_passphrase),
            signature_type=0,
        )
        print(f"      L2 key {creds.api_key[:12]}… (bound to the EOA — correct for EOA orders)")
    except Exception as e:
        _fail("authenticate", e); _classify(str(e)); return

    print("\n[2/3] Finding a live token...")
    market = _find_tradeable_token(client)
    if market is None:
        print("      ❌ no order-accepting token found"); return
    ref = market["price"]; tick = float(market["tick_size"])
    bid = max(tick, round(round(ref * 0.25 / tick) * tick, 6))
    n = round(max(1.0 / bid, float(market.get("min_order_size") or 5)), 2)
    print(f"      token={market['token_id'][:18]}…  market≈{ref:.3f}  bid={bid:.4f}  size={n}")

    print("\n[3/3] Posting EOA post_only BUY (won't fill)...")
    try:
        result = client.create_and_post_order(
            OrderArgsV2(token_id=market["token_id"], price=bid, size=n, side=BUY),
            options=PartialCreateOrderOptions(tick_size=market["tick_size"], neg_risk=market["neg_risk"]),
            post_only=True,
        )
        print(f"      RESPONSE: {result}")
        oid = result.get("orderID") or result.get("id")
        if oid:
            print(f"\n🎉 ORDER ACCEPTED id={oid} — EOA ordering fully works. Cancel it in the UI.")
        else:
            _classify(str(result))
    except Exception as e:
        print(f"      EXCEPTION: {type(e).__name__}: {e}")
        _classify(str(e))


if __name__ == "__main__":
    run()
