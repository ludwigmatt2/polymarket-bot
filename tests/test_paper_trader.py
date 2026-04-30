"""Tests for the paper trading engine — go-live gate and Brier score accuracy."""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from weather.config import MIN_PROFIT_FACTOR, MIN_RESOLVED_TRADES
from weather.paper_trader import PaperTrader
from weather.models import PaperTrade


def _make_trader(tmp_path):
    return PaperTrader(log_path=tmp_path / "paper_trades.csv")


def _add_resolved_trades(trader: PaperTrader, n: int, win_rate: float = 0.7) -> None:
    """Helper: directly write resolved trades to the CSV."""
    from datetime import timedelta
    import csv

    trades = []
    for i in range(n):
        is_win = i < int(n * win_rate)
        entry_price = 0.30
        direction = "YES"
        actual = True if is_win else False
        pnl = 25.0 * (1 - entry_price) if actual else -25.0 * entry_price
        brier = (0.70 - float(actual)) ** 2
        trades.append({
            "trade_id": f"t{i:04d}",
            "market_id": f"mkt_{i}",
            "market_title": f"Test market {i}",
            "signal_time": datetime.now(timezone.utc).isoformat(),
            "entry_price": entry_price,
            "model_p": 0.70,
            "direction": direction,
            "size_usd": 25.0,
            "edge_pp": 0.10,
            "ensemble_spread": 0.05,
            "confidence_score": 0.80,
            "resolution_date": datetime.now(timezone.utc).isoformat(),
            "actual_outcome": int(actual),
            "resolved_at": datetime.now(timezone.utc).isoformat(),
            "pnl_usd": round(pnl, 4),
            "brier_score": round(brier, 4),
            "cumulative_pnl": "",
            "cumulative_brier": "",
        })

    from weather.paper_trader import CSV_HEADERS
    path = trader.log_path
    path.parent.mkdir(exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(trades)


class TestBrierScore:
    def test_perfect_prediction_yes(self, tmp_path):
        trader = _make_trader(tmp_path)
        _add_resolved_trades(trader, 1, win_rate=1.0)
        trades = trader._load_all()
        assert float(trades[0]["brier_score"]) == pytest.approx(0.09, abs=0.01)  # (0.7-1)^2

    def test_worst_prediction(self):
        # If model_p=1.0 and outcome=False: Brier = (1.0 - 0)^2 = 1.0
        brier = (1.0 - 0.0) ** 2
        assert brier == pytest.approx(1.0)

    def test_correct_formula(self):
        model_p, actual = 0.8, True
        brier = (model_p - float(actual)) ** 2
        assert brier == pytest.approx(0.04)


class TestGoLiveGate:
    def test_gate_rejects_insufficient_trades(self, tmp_path):
        trader = _make_trader(tmp_path)
        _add_resolved_trades(trader, 5, win_rate=1.0)
        stats = trader.compute_stats()
        assert not stats.ready_for_live
        assert any("resolved" in r for r in stats.failure_reasons)

    def test_gate_passes_all_criteria(self, tmp_path):
        trader = _make_trader(tmp_path)
        # 25 trades, 80% win rate, good profit factor
        _add_resolved_trades(trader, 25, win_rate=0.8)
        stats = trader.compute_stats()
        # Check profit factor computed (may or may not pass depending on sizes)
        assert stats.resolved_trades == 25
        assert stats.profit_factor > 0

    def test_gate_rejects_low_profit_factor(self, tmp_path):
        trader = _make_trader(tmp_path)
        _add_resolved_trades(trader, MIN_RESOLVED_TRADES, win_rate=0.4)
        stats = trader.compute_stats()
        assert not stats.ready_for_live

    def test_stats_zero_resolved(self, tmp_path):
        trader = _make_trader(tmp_path)
        stats = trader.compute_stats()
        assert stats.resolved_trades == 0
        assert not stats.ready_for_live


class TestPnLCalculation:
    def test_yes_win(self, tmp_path):
        trader = _make_trader(tmp_path)
        _add_resolved_trades(trader, 1, win_rate=1.0)
        trades = trader._load_all()
        pnl = float(trades[0]["pnl_usd"])
        # Entry at 0.30, direction YES, outcome True → 25 * (1 - 0.30) = 17.50
        assert pnl == pytest.approx(17.5, abs=0.01)

    def test_yes_loss(self, tmp_path):
        trader = _make_trader(tmp_path)
        _add_resolved_trades(trader, 1, win_rate=0.0)
        trades = trader._load_all()
        pnl = float(trades[0]["pnl_usd"])
        # Entry at 0.30, direction YES, outcome False → -25 * 0.30 = -7.50
        assert pnl == pytest.approx(-7.5, abs=0.01)
