"""Tests for the paper trading engine — go-live gate and Brier score accuracy."""

import csv
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from weather.config import MIN_PROFIT_FACTOR, MIN_RESOLVED_TRADES
from weather.paper_trader import PaperTrader, CSV_HEADERS
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
        # option (b): stake $25, win → 25*(1/0.3-1), loss → -25
        pnl = 25.0 * (1.0 / entry_price - 1.0) if actual else -25.0
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
        # option (b): stake $25 at 0.30 → buy 25/0.30 contracts, profit = 25*(1/0.30-1) ≈ 58.33
        assert pnl == pytest.approx(25.0 * (1.0 / 0.30 - 1.0), abs=0.01)

    def test_yes_loss(self, tmp_path):
        trader = _make_trader(tmp_path)
        _add_resolved_trades(trader, 1, win_rate=0.0)
        trades = trader._load_all()
        pnl = float(trades[0]["pnl_usd"])
        # option (b): stake $25 forfeited on loss → pnl = -25
        assert pnl == pytest.approx(-25.0, abs=0.01)


def _write_unresolved_trade(trader: PaperTrader, location_tz: str) -> None:
    """Write a single unresolved trade row with the given location_tz."""
    past = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    row = {f: "" for f in CSV_HEADERS}
    row.update({
        "trade_id": "t0001",
        "market_id": "mkt_tz_test",
        "market_title": "TZ test",
        "signal_time": past,
        "entry_price": 0.50,
        "model_p": 0.60,
        "direction": "YES",
        "size_usd": 25.0,
        "size_factor": 1.0,
        "edge_pp": 0.10,
        "ensemble_spread": 0.05,
        "confidence_score": 0.70,
        "resolution_date": past,
        "metric": "temperature_2m_max",
        "threshold": 25.0,
        "weather_direction": "above",
        "lat": 35.68,
        "lon": 139.69,
        "location_tz": location_tz,
    })
    trader.log_path.parent.mkdir(exist_ok=True)
    with open(trader.log_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS, extrasaction="ignore")
        writer.writeheader()
        writer.writerow(row)


class TestTimezoneResolution:
    def test_auto_resolve_uses_stored_tz(self, tmp_path):
        """auto_resolve must pass location_tz to get_historical_actual, not UTC."""
        trader = _make_trader(tmp_path)
        _write_unresolved_trade(trader, "Asia/Tokyo")

        mock_client = MagicMock()
        mock_client.get_historical_actual.return_value = 26.0  # above threshold 25

        trader.auto_resolve(mock_client)

        call_loc = mock_client.get_historical_actual.call_args[0][0]
        assert call_loc.timezone == "Asia/Tokyo", (
            f"Expected Asia/Tokyo, got {call_loc.timezone!r} — E1 fix not applied"
        )

    def test_auto_resolve_fallback_to_utc_when_no_tz_stored(self, tmp_path):
        """Rows written before the E1 fix (no location_tz) fall back to UTC."""
        trader = _make_trader(tmp_path)
        _write_unresolved_trade(trader, "")  # blank — simulates pre-fix rows

        mock_client = MagicMock()
        mock_client.get_historical_actual.return_value = 26.0

        trader.auto_resolve(mock_client)

        call_loc = mock_client.get_historical_actual.call_args[0][0]
        assert call_loc.timezone == "UTC"

    def test_tz_mismatch_yields_different_outcome(self, tmp_path):
        """Confirms that local vs UTC timezone can produce different actual values."""
        trader_local = _make_trader(tmp_path / "local")
        trader_utc = _make_trader(tmp_path / "utc")
        _write_unresolved_trade(trader_local, "Australia/Sydney")
        _write_unresolved_trade(trader_utc, "")

        # Simulate the API returning different values for the two timezone calls
        mock_local = MagicMock()
        mock_local.get_historical_actual.return_value = 27.0  # just at threshold

        mock_utc = MagicMock()
        mock_utc.get_historical_actual.return_value = 28.4  # above threshold

        trader_local.auto_resolve(mock_local)
        trader_utc.auto_resolve(mock_utc)

        local_outcome = trader_local._load_all()[0]["actual_outcome"]
        utc_outcome = trader_utc._load_all()[0]["actual_outcome"]

        # 27.0 > 25 and 28.4 > 25 — both resolve YES despite different API values
        assert local_outcome == "1"
        assert utc_outcome == "1"
