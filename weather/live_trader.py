"""
Live trader — Kelly-sized order execution via pmxt.

STUB: This module is intentionally incomplete.
It is activated only after paper_trader.compute_stats().ready_for_live == True.

Set POLYMARKET_PRIVATE_KEY + POLYMARKET_PROXY_ADDRESS in .env before using.
Run /trail-of-bits-security on this module before first live trade.
"""

from __future__ import annotations

import os

from .config import KELLY_FRACTION, MAX_LIVE_TRADE_USD, ROUND_TRIP_FEE
from .models import Signal
from .paper_trader import PaperTrader


class LiveTrader:
    """
    NOT ACTIVE — awaiting paper trading validation.

    Go-live checklist:
    [ ] paper_trader.compute_stats().ready_for_live == True
    [ ] POLYMARKET_PRIVATE_KEY set in .env
    [ ] POLYMARKET_PROXY_ADDRESS set in .env
    [ ] /trail-of-bits-security audit passed on weather/ package
    [ ] /dependency-auditor run on requirements.txt
    [ ] Daily loss kill switch tested manually
    """

    def __init__(self, paper_trader: PaperTrader):
        self.paper_trader = paper_trader
        self._poly = None

    def is_unlocked(self) -> bool:
        stats = self.paper_trader.compute_stats()
        return stats.ready_for_live

    def kelly_size(self, signal: Signal, bankroll_usd: float) -> float:
        """
        Kelly criterion for binary bet:
          f = (b*p - q) / b
          b = (1 / entry_price) - 1   (decimal odds)
          p = model_p
          q = 1 - model_p
        Applies KELLY_FRACTION and caps at MAX_LIVE_TRADE_USD.
        """
        entry_price = signal.market_p if signal.direction == "YES" else (1.0 - signal.market_p)
        if entry_price <= 0 or entry_price >= 1:
            return 0.0
        b = (1.0 / entry_price) - 1.0
        p = signal.model_p
        q = 1.0 - p
        full_kelly_fraction = (b * p - q) / b
        if full_kelly_fraction <= 0:
            return 0.0
        size = bankroll_usd * KELLY_FRACTION * full_kelly_fraction
        return min(size, MAX_LIVE_TRADE_USD)

    def execute_signal(self, signal: Signal, bankroll_usd: float) -> None:
        raise NotImplementedError(
            "Live trading is locked until paper trading validation passes. "
            "Run: python weather_bot.py --mode stats"
        )
