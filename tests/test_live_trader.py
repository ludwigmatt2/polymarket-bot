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

    class _MarketOrderArgsV2:
        def __init__(self, token_id="", amount=0.0, side=None, price=0.0, order_type=None):
            self.token_id = token_id; self.amount = amount; self.side = side
            self.price = price; self.order_type = order_type

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
    _ct.MarketOrderArgsV2 = _MarketOrderArgsV2
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

    The live path now uses create_and_post_market_order and reads the fill from
    the response: for a BUY, takingAmount = shares received, makingAmount = USD
    spent (both plain decimals). `filled` is the shares; usd = filled * price.
    """
    mock = MagicMock()
    mock.create_and_post_market_order.return_value = {
        "orderID": "ord_mock",
        "takingAmount": str(filled),
        "makingAmount": str(round(filled * price, 2)),
        "status": status,
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

    def test_fill_read_from_response_no_scaling(self, tmp_path):
        """Shares come straight from takingAmount; price from makingAmount/shares."""
        trader = _make_trader(tmp_path, bankroll=500.0)
        mock = MagicMock()
        # 5.5 shares for $1.98 -> filled_price 0.36, no fixed-point scaling.
        mock.create_and_post_market_order.return_value = {
            "orderID": "ord_mock", "takingAmount": "5.5", "makingAmount": "1.98",
            "status": "partial",
        }
        trader._client = mock

        result = trader.execute_signal(_make_signal(market_p=0.35))

        assert result["filled"] == pytest.approx(5.5)
        rows = list(csv.DictReader(open(trader._log_path)))
        assert float(rows[0]["filled_size"]) == pytest.approx(5.5)
        assert float(rows[0]["filled_price"]) == pytest.approx(0.36, abs=0.001)

    def test_overspend_trips_guard(self, tmp_path):
        """makingAmount above the requested USD amount signals an API regression."""
        trader = _make_trader(tmp_path)
        trader.kelly_size_usd = lambda signal: 3.5  # amount_usd ~ $3.50
        mock = MagicMock()
        # Reports spending $100 — far above the ~$3.50 requested.
        mock.create_and_post_market_order.return_value = {
            "orderID": "ord_mock", "takingAmount": "285.0", "makingAmount": "100.0",
            "status": "filled",
        }
        trader._client = mock

        with pytest.raises(AssertionError, match="fill parsing"):
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


class TestMarketOrderAmount:
    def test_execute_signal_posts_usd_amount(self, tmp_path):
        """Market BUY must receive a USD amount (2-dec), not a contract count."""
        trader = _make_trader(tmp_path, bankroll=500.0)
        trader._client = _make_mock_client(filled=15.0)
        trader.kelly_size_usd = lambda signal: 10.0  # request $10

        trader.execute_signal(_make_signal(market_p=0.35, model_p=0.65))

        assert trader._client.create_and_post_market_order.called
        order_args = trader._client.create_and_post_market_order.call_args.args[0]
        assert order_args.amount == pytest.approx(10.0, abs=0.01)  # USD, not contracts
        assert order_args.side == "BUY"

    def test_slippage_cap_applied(self, tmp_path):
        """The market BUY carries a price cap above the entry (slippage guard)."""
        from weather.config import MAX_SLIPPAGE
        trader = _make_trader(tmp_path, bankroll=500.0)
        trader._client = _make_mock_client(filled=15.0)
        trader.kelly_size_usd = lambda signal: 10.0

        trader.execute_signal(_make_signal(market_p=0.35, model_p=0.65))

        order_args = trader._client.create_and_post_market_order.call_args.args[0]
        # ep 0.35, tick 0.01 -> cap ~0.36 (0.35 * 1.03 rounded to tick)
        assert 0.35 < order_args.price <= round(0.35 * (1 + MAX_SLIPPAGE) + 0.01, 4)


class TestMinOrderSize:
    def test_bumps_up_to_min_order_size_within_cap(self, tmp_path):
        """A sub-minimum order is bumped to the floor (in USD) when within cap."""
        trader = _make_trader(tmp_path)
        trader._client = _make_mock_client(filled=15.0)
        # ep = market_p = 0.35; size_usd 3.5 → 10 contracts, below a min of 15.
        trader.kelly_size_usd = lambda signal: 3.5
        signal = _make_signal(market_p=0.35, min_order_size=15.0)  # 15 * 0.35 = 5.25 ≤ 25 cap

        trader.execute_signal(signal)

        order_args = trader._client.create_and_post_market_order.call_args.args[0]
        # bumped USD = min_size * ep = 15 * 0.35 = 5.25
        assert order_args.amount == pytest.approx(5.25, abs=0.01)

    def test_skips_when_min_order_size_exceeds_cap(self, tmp_path):
        """If bumping to the floor would breach the per-trade cap, skip entirely."""
        trader = _make_trader(tmp_path)
        trader._client = _make_mock_client(filled=0.0)
        trader.kelly_size_usd = lambda signal: 3.5
        signal = _make_signal(market_p=0.35, min_order_size=100.0)  # 100 * 0.35 = 35 > 25 cap

        result = trader.execute_signal(signal)

        assert result is None
        trader._client.create_and_post_market_order.assert_not_called()

    def test_no_bump_when_already_above_min(self, tmp_path):
        """An order already at/above the floor keeps its USD amount."""
        trader = _make_trader(tmp_path)
        trader._client = _make_mock_client(filled=10.0)
        trader.kelly_size_usd = lambda signal: 3.5  # 10 contracts at ep 0.35
        signal = _make_signal(market_p=0.35, min_order_size=5.0)

        trader.execute_signal(signal)

        order_args = trader._client.create_and_post_market_order.call_args.args[0]
        assert order_args.amount == pytest.approx(3.5, abs=0.01)  # unchanged USD


class TestNegRisk:
    def test_neg_risk_passed_through_to_order_options(self, tmp_path):
        """neg_risk must come from the market (the book), not be hardcoded False."""
        trader = _make_trader(tmp_path)
        trader._client = _make_mock_client(filled=10.0)
        signal = _make_signal(market_p=0.35, model_p=0.65, neg_risk=True)

        trader.execute_signal(signal)

        options = trader._client.create_and_post_market_order.call_args.kwargs["options"]
        assert options.neg_risk is True


class TestOrderType:
    def test_live_order_submitted_as_fak(self, tmp_path):
        """Orders must be Fill-And-Kill, not resting GTC, to avoid stale fills."""
        trader = _make_trader(tmp_path)
        trader._client = _make_mock_client(filled=10.0)

        trader.execute_signal(_make_signal(market_p=0.35, model_p=0.65))

        order_type = trader._client.create_and_post_market_order.call_args.kwargs["order_type"]
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


# ---------------------------------------------------------------------------
# On-chain position reconciliation (Data API /positions)
# ---------------------------------------------------------------------------

class TestPositionsReconciliation:
    def test_fetch_positions_empty_without_wallet(self, tmp_path):
        trader = _make_trader(tmp_path)  # no funder_address
        assert trader.fetch_positions() == []

    def test_fetch_positions_parses_data_api(self, tmp_path, monkeypatch):
        trader = _make_trader(tmp_path)
        trader._funder_address = "0xproxy"
        payload = [{"conditionId": "0xabc", "outcome": "Yes", "size": 10.0, "redeemable": False}]

        class _Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return json.dumps(payload).encode()

        captured = {}
        def fake_urlopen(url, timeout=10):
            captured["url"] = url
            return _Resp()
        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

        positions = trader.fetch_positions()
        assert positions == payload
        assert "user=0xproxy" in captured["url"]

    def test_fetch_positions_returns_none_on_error(self, tmp_path, monkeypatch):
        """API failure returns None (distinct from a genuinely empty wallet)."""
        trader = _make_trader(tmp_path)
        trader._funder_address = "0xproxy"
        def boom(*a, **k):
            raise OSError("network down")
        monkeypatch.setattr("urllib.request.urlopen", boom)
        assert trader.fetch_positions() is None

    def test_reconcile_skips_when_snapshot_unavailable(self, tmp_path):
        """A failed positions fetch must not false-flag open local trades."""
        trader = _make_trader(tmp_path)
        trader.fetch_positions = lambda: None
        _write_live_trades(trader._log_path, [
            {"market_id": "0xABC", "direction": "YES", "order_id": "ord1", "actual_outcome": ""},
        ])
        assert trader.reconcile_positions() == []

    def test_redeemable_positions_filters(self, tmp_path):
        trader = _make_trader(tmp_path)
        trader.fetch_positions = lambda: [
            {"conditionId": "0xa", "redeemable": True},
            {"conditionId": "0xb", "redeemable": False},
            {"conditionId": "0xc", "redeemable": True},
        ]
        redeemable = trader.redeemable_positions()
        assert [p["conditionId"] for p in redeemable] == ["0xa", "0xc"]

    def test_reconcile_flags_missing_on_chain(self, tmp_path):
        """An open local trade with no matching on-chain position is flagged."""
        trader = _make_trader(tmp_path)
        trader.fetch_positions = lambda: []
        _write_live_trades(trader._log_path, [
            {"market_id": "0xABC", "direction": "YES", "order_id": "ord1", "actual_outcome": ""},
        ])
        div = trader.reconcile_positions()
        assert len(div) == 1
        assert div[0]["type"] == "missing_on_chain"
        assert div[0]["order_id"] == "ord1"

    def test_reconcile_flags_untracked_on_chain(self, tmp_path):
        """An on-chain position with no matching open local trade is flagged."""
        trader = _make_trader(tmp_path)
        trader.fetch_positions = lambda: [
            {"conditionId": "0xabc", "outcome": "Yes", "size": 5.0}
        ]
        _write_live_trades(trader._log_path, [])
        div = trader.reconcile_positions()
        assert len(div) == 1
        assert div[0]["type"] == "untracked_on_chain"
        assert div[0]["market_id"] == "0xabc"

    def test_reconcile_matches_case_insensitively(self, tmp_path):
        """A local trade matching an on-chain position (any case) yields no flag."""
        trader = _make_trader(tmp_path)
        trader.fetch_positions = lambda: [
            {"conditionId": "0xabc", "outcome": "Yes", "size": 10.0}
        ]
        _write_live_trades(trader._log_path, [
            {"market_id": "0xABC", "direction": "YES", "order_id": "ord1", "actual_outcome": ""},
        ])
        assert trader.reconcile_positions() == []

    def test_reconcile_ignores_resolved_local_trades(self, tmp_path):
        """Resolved local trades (actual_outcome set) are not reconciled."""
        trader = _make_trader(tmp_path)
        trader.fetch_positions = lambda: []
        _write_live_trades(trader._log_path, [
            {"market_id": "0xABC", "direction": "YES", "order_id": "ord1", "actual_outcome": "1"},
        ])
        assert trader.reconcile_positions() == []

    def test_print_divergences_renders_both_types(self, capsys):
        from weather.position_monitor import print_divergences
        print_divergences([
            {"type": "missing_on_chain", "market_id": "0xabcdef0123456789",
             "direction": "YES", "order_id": "ord12345"},
            {"type": "untracked_on_chain", "market_id": "0xfeed0000000000",
             "direction": "NO", "size": 7.5},
        ])
        out = capsys.readouterr().out
        assert "2 reconciliation divergence(s)" in out
        assert "local-only" in out and "on-chain-only" in out

    def test_print_divergences_clean_when_empty(self, capsys):
        from weather.position_monitor import print_divergences
        print_divergences([])
        assert "matches on-chain" in capsys.readouterr().out
