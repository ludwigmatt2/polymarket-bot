"""
Live trader — Kelly-sized order execution via pmxt.

Activated only when paper_trader.compute_stats().ready_for_live == True.
Set POLYMARKET_PRIVATE_KEY + POLYMARKET_PROXY_ADDRESS in .env before using.
"""

from __future__ import annotations

import csv
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import (
    DAILY_LOSS_LIMIT_PCT,
    KELLY_FRACTION,
    MAX_LIVE_TRADE_USD,
    ROUND_TRIP_FEE,
)
from .models import Signal
from .paper_trader import PaperTrader

LIVE_TRADES_LOG = Path("logs/live_trades.csv")

_CSV_HEADERS = [
    "trade_id", "market_id", "market_title", "order_id",
    "signal_time", "direction", "entry_price", "model_p",
    "size_usd", "kelly_fraction", "edge_pp",
    "submitted_at", "status", "error",
]


class LiveTrader:
    def __init__(self, paper_trader: PaperTrader, bankroll_usd: float):
        self.paper_trader = paper_trader
        self.bankroll_usd = bankroll_usd
        self._poly: Any = None

    def is_unlocked(self) -> bool:
        return self.paper_trader.compute_stats().ready_for_live

    def daily_pnl(self) -> float:
        """Live PnL today from live_trades.csv (resolved rows only)."""
        if not LIVE_TRADES_LOG.exists():
            return 0.0
        today = datetime.now(timezone.utc).date().isoformat()
        total = 0.0
        for row in csv.DictReader(open(LIVE_TRADES_LOG)):
            if row.get("submitted_at", "").startswith(today) and row.get("pnl_usd"):
                try:
                    total += float(row["pnl_usd"])
                except ValueError:
                    pass
        return total

    def kelly_size(self, signal: Signal) -> float:
        """Quarter-Kelly, capped at MAX_LIVE_TRADE_USD."""
        entry_price = signal.market_p if signal.direction == "YES" else (1.0 - signal.market_p)
        if not (0.0 < entry_price < 1.0):
            return 0.0
        b = (1.0 / entry_price) - 1.0
        p = signal.model_p
        full_kelly = (b * p - (1.0 - p)) / b
        if full_kelly <= 0:
            return 0.0
        return min(self.bankroll_usd * KELLY_FRACTION * full_kelly, MAX_LIVE_TRADE_USD)

    def execute_signal(self, signal: Signal) -> dict | None:
        """
        Place a limit order for signal. Returns order info dict or None if skipped.
        Raises RuntimeError on hard blocks (gates not passed, kill switch, bad creds).
        """
        if not self.is_unlocked():
            raise RuntimeError("Go-live gates not passed — run: python weather_bot.py dashboard")

        # Daily loss kill switch
        today_pnl = self.daily_pnl()
        if today_pnl < -(self.bankroll_usd * DAILY_LOSS_LIMIT_PCT):
            raise RuntimeError(
                f"Daily loss limit hit: {today_pnl:.2f} USD — halting until tomorrow"
            )

        size = self.kelly_size(signal)
        if size < 1.0:
            return None  # Too small to bother

        poly = self._get_poly()
        entry_price = signal.market_p if signal.direction == "YES" else (1.0 - signal.market_p)

        # Re-fetch market to get YES/NO token IDs
        mkt = poly.fetch_market(id=signal.market.market_id)
        outcome = mkt.yes if signal.direction == "YES" else mkt.no

        built = poly.build_order(
            market_id=signal.market.market_id,
            outcome_id=outcome.market_id,
            side="buy",
            type="limit",
            amount=round(size, 2),
            price=round(entry_price, 4),
        )
        order = poly.submit_order(built)
        order_id = str(getattr(order, "id", order))

        self._log_trade(signal, order_id, size, entry_price, status="submitted")

        # Mirror into paper log so resolve/stats stay in sync
        self.paper_trader.log_trade(signal)

        return {"order_id": order_id, "size_usd": size, "price": entry_price}

    def fetch_balance(self) -> float:
        """Return available USDC balance."""
        poly = self._get_poly()
        balances = poly.fetch_balance()
        for b in balances:
            if getattr(b, "currency", "") in ("USDC", "USDC.e", ""):
                try:
                    return float(getattr(b, "free", 0) or getattr(b, "total", 0))
                except (TypeError, ValueError):
                    pass
        return 0.0

    def _get_poly(self) -> Any:
        if self._poly is not None:
            return self._poly
        pk = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
        proxy = os.environ.get("POLYMARKET_PROXY_ADDRESS", "")
        if not pk or pk in ("0x...", ""):
            raise RuntimeError(
                "POLYMARKET_PRIVATE_KEY not set — add it to .env before going live"
            )
        import pmxt
        self._poly = pmxt.Polymarket(
            private_key=pk,
            proxy_address=proxy or None,
            signature_type="gnosis-safe" if proxy else "eoa",
        )
        return self._poly

    def _log_trade(
        self, signal: Signal, order_id: str, size: float, price: float, status: str
    ) -> None:
        is_new = not LIVE_TRADES_LOG.exists()
        LIVE_TRADES_LOG.parent.mkdir(exist_ok=True)
        with open(LIVE_TRADES_LOG, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=_CSV_HEADERS, extrasaction="ignore")
            if is_new:
                writer.writeheader()
            writer.writerow({
                "trade_id":     signal.market.market_id[:8],
                "market_id":    signal.market.market_id,
                "market_title": signal.market.title[:80],
                "order_id":     order_id,
                "signal_time":  signal.signal_time.isoformat(),
                "direction":    signal.direction,
                "entry_price":  round(price, 4),
                "model_p":      round(signal.model_p, 4),
                "size_usd":     round(size, 2),
                "kelly_fraction": round(KELLY_FRACTION, 3),
                "edge_pp":      round(signal.edge_pp, 4),
                "submitted_at": datetime.now(timezone.utc).isoformat(),
                "status":       status,
                "error":        "",
            })
