"""
Tests for LiveTrader — kill switch, fill reconciliation, sizing, idempotency.
All pmxt interactions are mocked; no network required.
"""

from __future__ import annotations

import csv
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from weather.config import DAILY_LOSS_LIMIT_PCT, KELLY_FRACTION, MAX_LIVE_TRADE_USD
from weather.live_trader import LiveTrader, _CSV_HEADERS
from weather.models import Location, Signal, WeatherMarket


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _make_signal(
    market_id: str = "mkt_abc123",
    direction: str = "YES",
    market_p: float = 0.35,
    model_p: float = 0.60,
) -> Signal:
    market = WeatherMarket(
        market_id=market_id,
        title=f"Test market {market_id[:6]}",
        yes_price=market_p,
        liquidity_usd=5000.0,
        resolution_date=datetime.now(timezone.utc) + timedelta(days=3),
        resolution_source="NOAA",
        location=Location(city="Berlin", lat=52.52, lon=13.41, timezone="Europe/Berlin"),
        metric="temperature_2m_max",
        threshold=25.0,
        direction="above",
        url="https://polymarket.com/test",
    )
    return Signal(
        market=market,
        model_p=model_p,
        market_p=market_p,
        edge_pp=abs(model_p - market_p),
        direction=direction,
        ensemble_spread=0.05,
        confidence_score=0.80,
        size_factor=1.0,
        quality_gate_passed=True,
        rejection_reason=None,
        signal_time=datetime.now(timezone.utc),
        forecast=MagicMock(),
        prob_result=MagicMock(),
    )


def _make_trader(tmp_path: Path, bankroll: float = 500.0) -> LiveTrader:
    """LiveTrader with I/O isolated to tmp_path and fill delay disabled."""
    paper = MagicMock()
    paper.compute_stats.return_value = MagicMock(ready_for_live=True)
    return LiveTrader(
        paper_trader=paper,
        bankroll_usd=bankroll,
        fill_poll_delay=0.0,
        log_path=tmp_path / "live_trades.csv",
        idempotency_path=tmp_path / "live_idempotency.json",
    )


def _make_mock_poly(filled: float, price: float = 0.35, status: str = "filled") -> MagicMock:
    """Minimal pmxt.Polymarket mock for execute_signal tests."""
    mock = MagicMock()
    order = MagicMock()
    order.id = "ord_mock"
    order_obj = MagicMock()
    order_obj.filled = filled
    order_obj.price = price
    order_obj.status = status
    mock.submit_order.return_value = order
    mock.fetch_order.return_value = order_obj
    mock.fetch_market.return_value = MagicMock(yes=MagicMock(market_id="yes_id"))
    mock.build_order.return_value = MagicMock()
    return mock


def _write_live_trades(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_HEADERS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# Session 3 tests — kill switch, fill reconciliation
# ---------------------------------------------------------------------------

class TestDailyPnl:
    def test_daily_pnl_sums_today_only(self, tmp_path):
        trader = _make_trader(tmp_path)
        today = datetime.now(timezone.utc).date().isoformat()
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()

        _write_live_trades(trader._log_path, [
            {"submitted_at": f"{today}T10:00:00+00:00", "pnl_usd": "12.50",
             "actual_outcome": "1", "resolved_at": f"{today}T14:00:00+00:00"},
            {"submitted_at": f"{today}T11:00:00+00:00", "pnl_usd": "-5.00",
             "actual_outcome": "0", "resolved_at": f"{today}T14:00:00+00:00"},
            {"submitted_at": f"{yesterday}T10:00:00+00:00", "pnl_usd": "999.00",
             "actual_outcome": "1", "resolved_at": f"{yesterday}T14:00:00+00:00"},
        ])

        assert trader.daily_pnl() == pytest.approx(7.50)

    def test_daily_pnl_zero_without_resolved_rows(self, tmp_path):
        trader = _make_trader(tmp_path)
        today = datetime.now(timezone.utc).date().isoformat()
        _write_live_trades(trader._log_path, [
            {"submitted_at": f"{today}T10:00:00+00:00", "pnl_usd": "",
             "actual_outcome": "", "resolved_at": ""},
        ])
        assert trader.daily_pnl() == pytest.approx(0.0)

    def test_daily_pnl_zero_when_no_file(self, tmp_path):
        trader = _make_trader(tmp_path)
        assert trader.daily_pnl() == pytest.approx(0.0)


class TestKillSwitch:
    def test_kill_switch_halts_on_5pct_loss(self, tmp_path):
        """daily_pnl below -5% bankroll must raise RuntimeError."""
        trader = _make_trader(tmp_path, bankroll=500.0)
        today = datetime.now(timezone.utc).date().isoformat()
        _write_live_trades(trader._log_path, [
            {"submitted_at": f"{today}T10:00:00+00:00", "pnl_usd": "-30.00",
             "actual_outcome": "0", "resolved_at": f"{today}T14:00:00+00:00"},
        ])

        with pytest.raises(RuntimeError, match="Daily loss limit hit"):
            trader.execute_signal(_make_signal())

    def test_kill_switch_allows_small_loss(self, tmp_path):
        """Loss under the threshold must not raise."""
        trader = _make_trader(tmp_path, bankroll=500.0)
        today = datetime.now(timezone.utc).date().isoformat()
        _write_live_trades(trader._log_path, [
            {"submitted_at": f"{today}T10:00:00+00:00", "pnl_usd": "-10.00",
             "actual_outcome": "0", "resolved_at": f"{today}T14:00:00+00:00"},
        ])
        trader._poly = _make_mock_poly(filled=10.0, price=0.35, status="filled")
        result = trader.execute_signal(_make_signal())
        assert result is not None
        assert result["status"] == "filled"


class TestFillReconciliation:
    def test_no_log_on_unfilled_order(self, tmp_path):
        """If the order never fills, nothing should be written to live_trades.csv."""
        trader = _make_trader(tmp_path)
        trader._poly = _make_mock_poly(filled=0.0, status="open")

        result = trader.execute_signal(_make_signal())

        assert result is not None
        assert result["status"] == "unfilled"
        assert result["filled"] == 0.0
        assert not trader._log_path.exists()
        trader.paper_trader.log_trade.assert_not_called()

    def test_partial_fill_logs_actual_size(self, tmp_path):
        """A partial fill should record the actual filled_size, not the requested size."""
        trader = _make_trader(tmp_path, bankroll=500.0)
        trader._poly = _make_mock_poly(filled=5.5, price=0.36, status="partial")

        trader.execute_signal(_make_signal(market_p=0.35))

        assert trader._log_path.exists()
        rows = list(csv.DictReader(open(trader._log_path)))
        assert len(rows) == 1
        assert float(rows[0]["filled_size"]) == pytest.approx(5.5)
        assert float(rows[0]["filled_price"]) == pytest.approx(0.36)
        assert rows[0]["order_status"] == "partial"

    def test_full_fill_mirrors_to_paper(self, tmp_path):
        """A fully-filled order should call paper_trader.log_trade exactly once."""
        trader = _make_trader(tmp_path)
        trader._poly = _make_mock_poly(filled=20.0)
        trader.execute_signal(_make_signal())
        trader.paper_trader.log_trade.assert_called_once()


# ---------------------------------------------------------------------------
# Session 4 tests — sizing, idempotency
# ---------------------------------------------------------------------------

class TestKellySizeUsd:
    def test_kelly_size_returns_usd_not_contracts(self):
        paper = MagicMock()
        trader = LiveTrader(paper_trader=paper, bankroll_usd=500.0, fill_poll_delay=0.0)
        signal = _make_signal(market_p=0.35, model_p=0.60)
        size = trader.kelly_size_usd(signal)
        assert 0.0 < size <= MAX_LIVE_TRADE_USD

    def test_kelly_size_zero_for_no_edge(self):
        paper = MagicMock()
        trader = LiveTrader(paper_trader=paper, bankroll_usd=500.0, fill_poll_delay=0.0)
        signal = _make_signal(market_p=0.60, model_p=0.60)
        assert trader.kelly_size_usd(signal) == 0.0


class TestContractConversion:
    def test_execute_signal_passes_contracts_to_build_order(self, tmp_path):
        """build_order must receive n_contracts = size_usd / entry_price, not raw USD."""
        trader = _make_trader(tmp_path, bankroll=500.0)
        trader._poly = _make_mock_poly(filled=15.0)

        signal = _make_signal(market_p=0.35, model_p=0.65)
        trader.execute_signal(signal)

        call_kwargs = trader._poly.build_order.call_args
        amount_passed = call_kwargs[1].get("amount") or call_kwargs[0][4]
        rows = list(csv.DictReader(open(trader._log_path)))
        size_usd = float(rows[0]["size_usd"])
        entry_price = float(rows[0]["entry_price"])
        expected_contracts = round(size_usd / entry_price, 2)
        assert amount_passed == pytest.approx(expected_contracts, abs=0.01)


class TestIdempotency:
    def test_execute_signal_skips_existing_unresolved_position(self, tmp_path):
        """If live_trades.csv already has an unresolved row for (market_id, direction), skip."""
        trader = _make_trader(tmp_path)
        signal = _make_signal(market_id="mkt_dupe", direction="YES")
        _write_live_trades(trader._log_path, [
            {
                "market_id": "mkt_dupe",
                "direction": "YES",
                "actual_outcome": "",
                "submitted_at": datetime.now(timezone.utc).isoformat(),
            }
        ])

        result = trader.execute_signal(signal)
        assert result is None
        trader.paper_trader.log_trade.assert_not_called()

    def test_execute_signal_skips_idempotency_key_match(self, tmp_path):
        """If the idempotency JSON already has a key for (market, direction, today), skip."""
        trader = _make_trader(tmp_path)
        signal = _make_signal(market_id="mkt_idem", direction="YES")
        today = date.today().isoformat()
        key = f"mkt_idem:YES:{today}"
        trader._idempotency_path.parent.mkdir(exist_ok=True)
        trader._idempotency_path.write_text(json.dumps({key: "ord_existing"}))

        result = trader.execute_signal(signal)
        assert result is None

    def test_idempotency_key_persisted_after_submit(self, tmp_path):
        """After a successful fill, the idempotency key must be written to the JSON file."""
        trader = _make_trader(tmp_path)
        trader._poly = _make_mock_poly(filled=10.0)

        signal = _make_signal(market_id="mkt_newkey", direction="YES")
        trader.execute_signal(signal)

        assert trader._idempotency_path.exists()
        keys = json.loads(trader._idempotency_path.read_text())
        today = date.today().isoformat()
        assert f"mkt_newkey:YES:{today}" in keys
