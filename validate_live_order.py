"""
Validate Phases 1+2 at the WRITE level: drive the REAL LiveTrader.execute_signal
(market-order path + slippage cap + fill parsing + logging) using the stored
admin creds (sig=3 + deposit-wallet funder), against a live liquid market.

Buys ~$1 via the actual bot code path (not a spike), confirms the fill + log +
on-chain position. Isolated to a temp log dir so prod live_trades.csv is untouched.
Exit the position afterwards with: venv/bin/python spike_sell_order.py

Run from the VPS:
  venv/bin/python validate_live_order.py
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

from dotenv import load_dotenv

load_dotenv("/opt/polymarket-bot/.env")

from spike_eoa import _find_tradeable_token
from weather.models import Location, Signal, WeatherMarket
from weather.live_trader import LiveTrader
from weather.secrets import get_user_creds

SPEND = float(os.environ.get("SPEND_USD", "1.10"))


def run() -> None:
    uid = int(os.environ["ADMIN_ID"])
    c = get_user_creds(uid)
    if not c:
        print("❌ no admin creds"); return
    print(f"creds: sig={c.get('signature_type')} funder={c.get('funder_address')}")

    tmp = Path(tempfile.mkdtemp(prefix="validate_live_"))
    paper = MagicMock()
    paper.compute_stats.return_value = MagicMock(ready_for_live=True)
    trader = LiveTrader.from_creds(
        c, paper_trader=paper, bankroll_usd=100.0,
        log_path=tmp / "live_trades.csv", idempotency_path=tmp / "idem.json",
    )

    print("Finding a live token...")
    mk = _find_tradeable_token(trader._get_client())
    if not mk:
        print("❌ no liquid token"); return
    price = mk["price"]
    print(f"  token={mk['token_id'][:18]}…  price={price:.3f}  tick={mk['tick_size']}  neg_risk={mk['neg_risk']}")

    # Minimal but complete Signal for a YES buy on this token.
    market = WeatherMarket(
        market_id=mk["condition_id"], title="LIVE VALIDATION (bot path)",
        yes_price=price, liquidity_usd=5000.0,
        resolution_date=datetime.now(timezone.utc) + timedelta(days=2),
        resolution_source="NOAA",
        location=Location(city="Berlin", lat=52.52, lon=13.41, timezone="Europe/Berlin"),
        metric="temperature_2m_max", threshold=25.0, direction="above",
        url="https://polymarket.com", yes_token_id=mk["token_id"], no_token_id="",
        tick_size=str(mk["tick_size"]), min_order_size=float(mk.get("min_order_size") or 0),
        neg_risk=bool(mk["neg_risk"]),
    )
    signal = Signal(
        market=market, model_p=min(price + 0.10, 0.95), market_p=price,
        edge_pp=0.10, direction="YES", ensemble_spread=0.05, confidence_score=0.8,
        size_factor=1.0, quality_gate_passed=True, rejection_reason=None,
        signal_time=datetime.now(timezone.utc), forecast=MagicMock(), prob_result=MagicMock(),
    )

    # Drive the REAL execute_signal; force the stake to ~$1 (kelly math is unit-tested
    # separately — this validates the order/fill path).
    trader.kelly_size_usd = lambda s: SPEND
    print(f"\nCalling LiveTrader.execute_signal (real bot path, ~${SPEND})...")
    result = trader.execute_signal(signal)
    print(f"RESULT: {result}")

    if result and result.get("filled", 0) > 0:
        print(f"\n✅ BOT-PATH LIVE ORDER FILLED: {result['filled']} shares @ "
              f"{result['filled_price']:.3f}  (${result['size_usd']:.2f})")
        rows = list(open(trader._log_path))
        print(f"   live_trades.csv rows: {len(rows)-1} (logged)")
        pos = trader.fetch_positions() or []
        print(f"   on-chain positions: {len(pos)}")
        print("\nExit with: venv/bin/python spike_sell_order.py")
    else:
        print("\n⚠️ not filled — check the result above")


if __name__ == "__main__":
    run()
