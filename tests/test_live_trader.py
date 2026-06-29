"""
Tests for LiveTrader — kill switch, fill reconciliation, sizing, idempotency.
All ClobClient interactions are mocked; no network required.
"""

from __future__ import annotations

import csv
import json
import sys
import types as _t
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Stub py_clob_client_v2 if not installed — keeps tests runnable in envs
# without the real SDK while still allowing call_args inspection.
# ---------------------------------------------------------------------------
if "py_clob_client_v2" not in sys.modules:
    class _OrderArgsV2:
        def __init__(self, token_id="", price=0.0, size=0.0, side=None):
            self.token_id = token_id; self.price = price
            self.size = size; self.side = side

    class _PartialCreateOrderOptions:
        def __init__(self, tick_size="0.01", neg_risk=False):
            self.tick_size = tick_size; self.neg_risk = neg_risk

    class _BalanceAllowanceParams:
        def __init__(self, asset_type=None): pass

    class _AssetType:
        COLLATERAL = "COLLATERAL"

    class _ApiCreds:
        def __init__(self, api_key="", api_secret="", api_passphrase=""): pass

    class _OrderType:
        GTC = "GTC"; FOK = "FOK"; GTD = "GTD"; FAK = "FAK"

    _ct = _t.ModuleType("py_clob_client_v2.clob_types")
    _ct.OrderArgsV2 = _OrderArgsV2
    _ct.PartialCreateOrderOptions = _PartialCreateOrderOptions
    _ct.BalanceAllowanceParams = _BalanceAllowanceParams
    _ct.AssetType = _AssetType
    _ct.ApiCreds = _ApiCreds
    _ct.OrderType = _OrderType

    _ob_const = _t.ModuleType("py_clob_client_v2.order_builder.constants")
    _ob_const.BUY = "BUY"
    _ob = _t.ModuleType("py_clob_client_v2.order_builder")

    _root = _t.ModuleType("py_clob_client_v2")
    _root.ClobClient = MagicMock
    _root.clob_types = _ct

    sys.modules["py_clob_client_v2"] = _root
    sys.modules["py_clob_client_v2.clob_types"] = _ct
    sys.modules["py_clob_client_v2.order_builder"] = _ob
    sys.modules["py_clob_client_v2.order_builder.constants"] = _ob_const

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
    min_order_size: float = 0.0,
    neg_risk: bool = False,
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
        yes_token_id="yes_token_id",
        no_token_id="no_token_id",
        tick_size="0.01",
        min_order_size=min_order_size,
        neg_risk=neg_risk,
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


def _make_mock_client(filled: float, price: float = 0.35, status: str = "filled") -> MagicMock:
    """Minimal ClobClient mock for execute_signal tests.

    `filled` is the contract count; the real API reports size_matched in
    6-decimal fixed-math, so the mock emits it that way (filled * 1e6).
    """
    mock = MagicMock()
    mock.create_and_post_order.return_value = {"orderID": "ord_mock"}
    mock.get_order.return_value = {
        "size_matched": filled * 1e6, "price": price, "status": status
    }
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
        trader._client = _make_mock_client(filled=10.0, price=0.35, status="filled")
        result = trader.execute_signal(_make_signal())
        assert result is not None
        assert result["status"] == "filled"


class TestFillReconciliation:
    def test_no_log_on_unfilled_order(self, tmp_path):
        """If the order never fills, nothing should be written to live_trades.csv."""
        trader = _make_trader(tmp_path)
        trader._client = _make_mock_client(filled=0.0, status="open")

        result = trader.execute_signal(_make_signal())

        assert result is not None
        assert result["status"] == "unfilled"
        assert result["filled"] == 0.0
        assert not trader._log_path.exists()
        trader.paper_trader.log_trade.assert_not_called()

    def test_partial_fill_logs_actual_size(self, tmp_path):
        """A partial fill should record the actual filled_size, not the requested size."""
        trader = _make_trader(tmp_path, bankroll=500.0)
        trader._client = _make_mock_client(filled=5.5, price=0.36, status="partial")

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
        trader._client = _make_mock_client(filled=20.0)
        trader.execute_signal(_make_signal())
        trader.paper_trader.log_trade.assert_called_once()

    def test_size_matched_fixed_math_is_normalized(self, tmp_path):
        """Raw 6-decimal fixed-math size_matched must be divided down to contracts."""
        trader = _make_trader(tmp_path, bankroll=500.0)
        mock = MagicMock()
        mock.create_and_post_order.return_value = {"orderID": "ord_mock"}
        # Raw API shape: "5500000" == 5.5 contracts (not 5.5 million).
        mock.get_order.return_value = {"size_matched": "5500000", "price": "0.36", "status": "partial"}
        trader._client = mock

        result = trader.execute_signal(_make_signal(market_p=0.35))

        assert result["filled"] == pytest.approx(5.5)
        rows = list(csv.DictReader(open(trader._log_path)))
        assert float(rows[0]["filled_size"]) == pytest.approx(5.5)

    def test_impossible_fill_trips_guard(self, tmp_path):
        """A fill exceeding the order size signals a scaling/API regression — fail loud."""
        trader = _make_trader(tmp_path)
        trader.kelly_size_usd = lambda signal: 3.5  # ~10 contracts at ep 0.35
        mock = MagicMock()
        mock.create_and_post_order.return_value = {"orderID": "ord_mock"}
        # 1e9 fixed-math == 1000 contracts, far above the ~10 ordered.
        mock.get_order.return_value = {"size_matched": "1000000000", "price": "0.35", "status": "filled"}
        trader._client = mock

        with pytest.raises(AssertionError, match="size_matched scaling"):
            trader.execute_signal(_make_signal(market_p=0.35))


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
    def test_execute_signal_posts_contracts_not_usd(self, tmp_path):
        """create_and_post_order must receive n_contracts = size_usd / entry_price, not raw USD."""
        trader = _make_trader(tmp_path, bankroll=500.0)
        trader._client = _make_mock_client(filled=15.0)

        signal = _make_signal(market_p=0.35, model_p=0.65)
        trader.execute_signal(signal)

        assert trader._client.create_and_post_order.called
        # First positional arg is the OrderArgsV2 instance
        order_args = trader._client.create_and_post_order.call_args.args[0]
        rows = list(csv.DictReader(open(trader._log_path)))
        size_usd = float(rows[0]["size_usd"])
        entry_price = float(rows[0]["entry_price"])
        expected_contracts = round(size_usd / entry_price, 2)
        assert order_args.size == pytest.approx(expected_contracts, abs=0.01)


class TestMinOrderSize:
    def test_bumps_up_to_min_order_size_within_cap(self, tmp_path):
        """A sub-minimum order is bumped to the floor when the stake stays in cap."""
        trader = _make_trader(tmp_path)
        trader._client = _make_mock_client(filled=15.0)
        # ep = market_p = 0.35; size_usd 3.5 → 10 contracts, below a min of 15.
        trader.kelly_size_usd = lambda signal: 3.5
        signal = _make_signal(market_p=0.35, min_order_size=15.0)  # 15 * 0.35 = 5.25 ≤ 25 cap

        trader.execute_signal(signal)

        order_args = trader._client.create_and_post_order.call_args.args[0]
        assert order_args.size == pytest.approx(15.0, abs=0.01)

    def test_skips_when_min_order_size_exceeds_cap(self, tmp_path):
        """If bumping to the floor would breach the per-trade cap, skip entirely."""
        trader = _make_trader(tmp_path)
        trader._client = _make_mock_client(filled=0.0)
        trader.kelly_size_usd = lambda signal: 3.5
        signal = _make_signal(market_p=0.35, min_order_size=100.0)  # 100 * 0.35 = 35 > 25 cap

        result = trader.execute_signal(signal)

        assert result is None
        trader._client.create_and_post_order.assert_not_called()

    def test_no_bump_when_already_above_min(self, tmp_path):
        """An order already at/above the floor is posted unchanged."""
        trader = _make_trader(tmp_path)
        trader._client = _make_mock_client(filled=10.0)
        trader.kelly_size_usd = lambda signal: 3.5  # 10 contracts at ep 0.35
        signal = _make_signal(market_p=0.35, min_order_size=5.0)

        trader.execute_signal(signal)

        order_args = trader._client.create_and_post_order.call_args.args[0]
        assert order_args.size == pytest.approx(10.0, abs=0.01)


class TestNegRisk:
    def test_neg_risk_passed_through_to_order_options(self, tmp_path):
        """neg_risk must come from the market (the book), not be hardcoded False."""
        trader = _make_trader(tmp_path)
        trader._client = _make_mock_client(filled=10.0)
        signal = _make_signal(market_p=0.35, model_p=0.65, neg_risk=True)

        trader.execute_signal(signal)

        options = trader._client.create_and_post_order.call_args.kwargs["options"]
        assert options.neg_risk is True


class TestOrderType:
    def test_live_order_submitted_as_fak(self, tmp_path):
        """Orders must be Fill-And-Kill, not resting GTC, to avoid stale fills."""
        trader = _make_trader(tmp_path)
        trader._client = _make_mock_client(filled=10.0)

        trader.execute_signal(_make_signal(market_p=0.35, model_p=0.65))

        order_type = trader._client.create_and_post_order.call_args.kwargs["order_type"]
        assert order_type == "FAK"


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
        trader._client = _make_mock_client(filled=10.0)

        signal = _make_signal(market_id="mkt_newkey", direction="YES")
        trader.execute_signal(signal)

        assert trader._idempotency_path.exists()
        keys = json.loads(trader._idempotency_path.read_text())
        today = date.today().isoformat()
        assert f"mkt_newkey:YES:{today}" in keys


# ---------------------------------------------------------------------------
# Phase 4 — per-user constructor credentials
# ---------------------------------------------------------------------------

class TestPerUserCredentials:
    def _patch_clob(self, monkeypatch) -> dict:
        """Patch ClobClient to capture constructor kwargs; return the capture dict."""
        captured = {}

        class FakeClobClient:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        import types as _types
        fake_root = _types.ModuleType("py_clob_client_v2")
        fake_root.ClobClient = FakeClobClient
        monkeypatch.setitem(sys.modules, "py_clob_client_v2", fake_root)
        return captured

    def test_constructor_creds_passed_to_clob_client(self, tmp_path, monkeypatch):
        """ClobClient must receive key, funder, and integer signature_type from constructor."""
        captured = self._patch_clob(monkeypatch)
        trader = LiveTrader(
            paper_trader=MagicMock(), bankroll_usd=100,
            log_path=tmp_path / "lt.csv", idempotency_path=tmp_path / "id.json",
            private_key="0xuserkey", funder_address="0xuserproxy",
            signature_type="gnosis-safe",  # legacy string → 1 (POLY_PROXY)
        )
        trader._get_client()
        assert captured["key"] == "0xuserkey"
        assert captured["funder"] == "0xuserproxy"
        assert captured["signature_type"] == 1  # "gnosis-safe" → POLY_PROXY → 1

    def test_constructor_creds_used(self, tmp_path, monkeypatch):
        """Credentials must come from the constructor, not env vars."""
        monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0xenvkey")  # must be ignored
        captured = self._patch_clob(monkeypatch)
        trader = LiveTrader(
            paper_trader=MagicMock(), bankroll_usd=100,
            log_path=tmp_path / "lt.csv", idempotency_path=tmp_path / "id.json",
            private_key="0xconstructorkey",
        )
        trader._get_client()
        assert captured["key"] == "0xconstructorkey"
        assert captured["signature_type"] == 0  # no funder → EOA → 0

    def test_no_key_anywhere_raises(self, tmp_path, monkeypatch):
        monkeypatch.delenv("POLYMARKET_PRIVATE_KEY", raising=False)
        trader = LiveTrader(
            paper_trader=MagicMock(), bankroll_usd=100,
            log_path=tmp_path / "lt.csv", idempotency_path=tmp_path / "id.json",
        )
        with pytest.raises(RuntimeError, match="private key"):
            trader._get_client()


# ---------------------------------------------------------------------------
# Geoblock pre-flight check
# ---------------------------------------------------------------------------

class TestGeoblock:
    def _reset_cache(self):
        import weather.live_trader as lt
        lt._geoblock_cache = None

    def test_returns_parsed_payload(self, monkeypatch):
        self._reset_cache()
        import weather.live_trader as lt
        resp = MagicMock()
        resp.json.return_value = {"blocked": True, "country": "DE", "region": "BY", "ip": "1.2.3.4"}
        resp.raise_for_status.return_value = None
        monkeypatch.setattr("requests.get", lambda *a, **k: resp)
        geo = lt.check_geoblock()
        assert geo["blocked"] is True and geo["country"] == "DE"

    def test_none_on_failure_not_cached(self, monkeypatch):
        self._reset_cache()
        import weather.live_trader as lt
        def boom(*a, **k):
            raise OSError("network down")
        monkeypatch.setattr("requests.get", boom)
        assert lt.check_geoblock() is None
        assert lt._geoblock_cache is None  # failures are not cached → retried next call

    def test_success_is_cached(self, monkeypatch):
        self._reset_cache()
        import weather.live_trader as lt
        calls = {"n": 0}
        def once(*a, **k):
            calls["n"] += 1
            r = MagicMock()
            r.json.return_value = {"blocked": False, "country": "IE"}
            r.raise_for_status.return_value = None
            return r
        monkeypatch.setattr("requests.get", once)
        lt.check_geoblock()
        lt.check_geoblock()
        assert calls["n"] == 1  # second call served from cache
