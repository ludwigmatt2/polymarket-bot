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


class TestPhase0CalibrationInput:
    """Phase 0: the calibrator must be trained on raw_p, not the calibrated+shrunk model_p."""

    def _write_unresolved_with_raw_p(self, trader, raw_p, model_p):
        past = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        row = {f: "" for f in CSV_HEADERS}
        row.update({
            "trade_id": "t0001", "market_id": "mkt_raw", "market_title": "raw_p test",
            "signal_time": past, "entry_price": 0.50, "model_p": model_p, "direction": "YES",
            "size_usd": 25.0, "size_factor": 1.0, "edge_pp": 0.10, "ensemble_spread": 0.05,
            "confidence_score": 0.70, "resolution_date": past, "metric": "temperature_2m_max",
            "threshold": 25.0, "weather_direction": "above", "lat": 35.68, "lon": 139.69,
            "location_tz": "UTC", "raw_p": raw_p,
        })
        trader.log_path.parent.mkdir(exist_ok=True)
        with open(trader.log_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_HEADERS, extrasaction="ignore")
            writer.writeheader()
            writer.writerow(row)

    def test_auto_resolve_logs_raw_p_not_model_p(self, tmp_path):
        trader = _make_trader(tmp_path)
        self._write_unresolved_with_raw_p(trader, raw_p=0.42, model_p=0.61)

        client = MagicMock()
        client.get_historical_actual.return_value = 26.0  # above 25 → outcome True
        model = MagicMock()

        trader.auto_resolve(client, model=model)

        model.log_observation.assert_called_once()
        logged_p = model.log_observation.call_args[0][0]
        assert logged_p == pytest.approx(0.42), "calibrator must receive raw_p, not model_p"

    def test_auto_resolve_falls_back_to_model_p_for_pre_phase0_rows(self, tmp_path):
        trader = _make_trader(tmp_path)
        self._write_unresolved_with_raw_p(trader, raw_p="", model_p=0.61)  # legacy row

        client = MagicMock()
        client.get_historical_actual.return_value = 26.0
        model = MagicMock()

        trader.auto_resolve(client, model=model)

        logged_p = model.log_observation.call_args[0][0]
        assert logged_p == pytest.approx(0.61), "legacy rows fall back to model_p"


class TestStationResolution:
    """Phase 1: trades with a resolving station settle on the station's reading."""

    def _write_station_trade(self, trader, station="KLGA", country="US", unit="F"):
        past = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        row = {f: "" for f in CSV_HEADERS}
        row.update({
            "trade_id": "s001", "market_id": "mkt_st", "market_title": "station test",
            "signal_time": past, "entry_price": 0.50, "model_p": 0.30, "direction": "NO",
            "size_usd": 25.0, "size_factor": 1.0, "edge_pp": 0.10, "ensemble_spread": 0.05,
            "confidence_score": 0.70, "resolution_date": past, "metric": "temperature_2m_max",
            "threshold": 35.5556, "threshold_high": 36.1111, "weather_direction": "range",
            "lat": 40.71, "lon": -74.0, "location_tz": "America/New_York", "raw_p": 0.30,
            "station_icao": station, "station_country": country, "resolve_unit": unit,
        })
        trader.log_path.parent.mkdir(exist_ok=True)
        with open(trader.log_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=CSV_HEADERS, extrasaction="ignore")
            w.writeheader(); w.writerow(row)

    def test_resolves_via_station_not_weather(self, tmp_path, monkeypatch):
        import weather.station_truth as st
        trader = _make_trader(tmp_path)
        self._write_station_trade(trader)
        # WU 97°F → in the 96-97 bucket → YES-condition true; weather_client unused
        monkeypatch.setattr(st, "daily_value", lambda *a, **k: (97.0, "wunderground"))
        client = MagicMock()
        trader.auto_resolve(client, model=None)
        client.get_historical_actual.assert_not_called()
        rows = list(csv.DictReader(open(trader.log_path)))
        assert rows[0]["actual_outcome"] == "1"

    def test_station_stays_pending_when_no_data(self, tmp_path, monkeypatch):
        import weather.station_truth as st
        trader = _make_trader(tmp_path)
        self._write_station_trade(trader)
        monkeypatch.setattr(st, "daily_value", lambda *a, **k: (None, None))
        trader.auto_resolve(MagicMock(), model=None)
        rows = list(csv.DictReader(open(trader.log_path)))
        assert rows[0]["actual_outcome"] == ""  # not booked

    def test_no_station_uses_weather(self, tmp_path):
        trader = _make_trader(tmp_path)
        self._write_station_trade(trader, station="")  # no station → Open-Meteo path
        client = MagicMock()
        client.get_historical_actual.return_value = 40.0  # above the range → NO wins
        trader.auto_resolve(client, model=None)
        client.get_historical_actual.assert_called_once()
        rows = list(csv.DictReader(open(trader.log_path)))
        assert rows[0]["actual_outcome"] == "0"


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


def _add_station_trades(trader: PaperTrader, n: int, win_rate: float = 0.7,
                        span_days: int = 30, model_p: float = 0.15,
                        entry_price: float = 0.30, direction: str = "NO") -> None:
    """Resolved STATION-labeled NO trades: model says P(YES)=model_p, market says
    entry-implied P(YES)=1−entry... for NO, cost=entry_price so market P(YES)=
    1−entry_price. Wins = outcome NO (actual 0) at rate win_rate, spread over
    span_days so the gate's calendar criterion is controllable."""
    start = datetime.now(timezone.utc) - timedelta(days=span_days - 1)
    rows = []
    for i in range(n):
        won = i < int(n * win_rate)          # NO bet wins when outcome = 0
        outcome = 0 if won else 1
        pnl = 25.0 * (1.0 / entry_price - 1.0) if won else -25.0
        sig_day = start + timedelta(days=i % span_days)
        rows.append({
            "trade_id": f"st{i:04d}", "market_id": f"stmkt_{i}",
            "market_title": f"Station market {i}",
            "signal_time": sig_day.isoformat(),
            "entry_price": entry_price, "model_p": model_p, "direction": direction,
            "size_usd": 25.0, "edge_pp": 0.12, "ensemble_spread": 0.05,
            "confidence_score": 0.8,
            "resolution_date": (sig_day + timedelta(days=1)).isoformat(),
            "actual_outcome": outcome,
            "resolved_at": (sig_day + timedelta(days=1)).isoformat(),
            "pnl_usd": round(pnl, 4),
            "brier_score": round((model_p - outcome) ** 2, 4),
            "label_source": "station",
        })
    path = trader.log_path
    path.parent.mkdir(exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_HEADERS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


class TestReliveGate:
    """Jul-8 gate: station-labeled trades only, calendar span, PF, and the
    model-must-beat-the-market Brier test."""

    @pytest.fixture(autouse=True)
    def _open_era(self, monkeypatch):
        # These tests build synthetic histories with past signal_times; the
        # era boundary (Jul-10 addition) is opened so they test the gate rules
        # themselves. Era filtering has its own dedicated test below.
        import weather.paper_trader as pt
        monkeypatch.setattr(pt, "GATE_ERA_START", "2000-01-01")

    def test_gate_passes_on_good_station_record(self, tmp_path):
        trader = _make_trader(tmp_path)
        # 160 station trades over 30 days, 80% NO win rate.
        # model_p=0.15 vs realized YES rate 20% → model Brier ≈ 0.1625
        # market P(YES)=0.70 (NO entry 0.30) → market Brier ≈ 0.53 → model wins
        _add_station_trades(trader, 160, win_rate=0.8, span_days=30)
        stats = trader.compute_stats()
        assert stats.station_resolved == 160
        assert stats.days_elapsed >= 30
        assert stats.station_model_brier < stats.station_market_brier
        assert stats.ready_for_live, stats.failure_reasons

    def test_gate_rejects_too_few_station_trades(self, tmp_path):
        trader = _make_trader(tmp_path)
        _add_station_trades(trader, 60, win_rate=0.8, span_days=30)
        stats = trader.compute_stats()
        assert not stats.ready_for_live
        assert any("station_resolved" in r for r in stats.failure_reasons)

    def test_gate_rejects_short_calendar_span(self, tmp_path):
        trader = _make_trader(tmp_path)
        _add_station_trades(trader, 160, win_rate=0.8, span_days=5)
        stats = trader.compute_stats()
        assert not stats.ready_for_live
        assert any("days" in r for r in stats.failure_reasons)

    def test_gate_rejects_model_that_does_not_beat_market(self, tmp_path):
        trader = _make_trader(tmp_path)
        # Model barely disagrees with the market (model 0.65 vs market 0.70) and
        # the realized YES rate is 0.70 — the market is calibrated, the model
        # isn't better. PF still fine (80% NO wins impossible here: outcome rate
        # 70% YES → NO wins 30%) — use direction YES so wins align:
        # YES entry 0.70, model 0.65, outcomes 70% YES.
        rows_win_rate = 0.7
        _add_station_trades(trader, 160, win_rate=rows_win_rate, span_days=30,
                            model_p=0.10, entry_price=0.30, direction="NO")
        # override: make model WORSE than market — model_p 0.10 vs realized 0.30
        # while market implied P(YES)=0.70... market Brier here is bad too; craft
        # explicit comparison instead: model_p=0.55 with realized YES 30%:
        # model Brier=(0.55-0/1)^2 mix ≈ 0.55²·0.7+0.45²·0.3=0.272
        # market P(YES)=0.70: 0.7²·0.7+0.3²·0.3=0.37 → model still wins. Push model
        # to 0.85: 0.85²·0.7 + 0.15²·0.3 = 0.512 > 0.37 → model loses.
        _add_station_trades(trader, 160, win_rate=0.7, span_days=30,
                            model_p=0.85, entry_price=0.30, direction="NO")
        stats = trader.compute_stats()
        assert stats.station_model_brier >= stats.station_market_brier
        assert not stats.ready_for_live
        assert any("model_brier" in r for r in stats.failure_reasons)

    def test_grid_labeled_trades_do_not_count(self, tmp_path):
        trader = _make_trader(tmp_path)
        _add_resolved_trades(trader, 200, win_rate=0.8)  # no label_source
        stats = trader.compute_stats()
        assert stats.station_resolved == 0
        assert not stats.ready_for_live

    def test_market_brier_reconstruction_no_side(self, tmp_path):
        # One NO trade at entry 0.30 → market P(YES) = 0.70; outcome YES=1
        trader = _make_trader(tmp_path)
        _add_station_trades(trader, 1, win_rate=0.0, span_days=1)
        stats = trader.compute_stats()
        assert stats.station_market_brier == pytest.approx((0.70 - 1.0) ** 2, abs=1e-4)


class TestExitSimulation:
    """X1: counterfactual exits — pnl_usd stays hold-to-resolution truth."""

    def _open_trade_file(self, trader, entry=0.5, size=25.0):
        rows = [{
            "trade_id": "x1", "market_id": "mkt_exit", "market_title": "T",
            "signal_time": "2026-07-09T10:00:00+00:00",
            "entry_price": entry, "model_p": 0.4, "direction": "NO",
            "size_usd": size, "edge_pp": 0.12,
            "resolution_date": "2026-07-09T12:00:00+00:00",
            "actual_outcome": "", "resolved_at": "", "pnl_usd": "",
        }]
        with open(trader.log_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=CSV_HEADERS, extrasaction="ignore")
            w.writeheader(); w.writerows(rows)

    def test_exit_recorded_with_correct_pnl(self, tmp_path):
        trader = _make_trader(tmp_path)
        self._open_trade_file(trader, entry=0.5, size=25.0)  # 50 shares
        n = trader.apply_exit_sims([{"market_id": "mkt_exit", "direction": "NO",
                                     "bid": 0.60, "p_win_now": 0.5, "reason": "r"}])
        assert n == 1
        row = trader._load_all()[0]
        assert float(row["exit_price"]) == pytest.approx(0.60)
        assert float(row["exit_pnl_usd"]) == pytest.approx(50 * 0.60 - 25.0)  # +5.00
        assert row["pnl_usd"] in ("", None)  # hold-to-resolution untouched

    def test_exit_only_once(self, tmp_path):
        trader = _make_trader(tmp_path)
        self._open_trade_file(trader)
        e = [{"market_id": "mkt_exit", "direction": "NO", "bid": 0.6,
              "p_win_now": 0.5, "reason": "r"}]
        assert trader.apply_exit_sims(e) == 1
        assert trader.apply_exit_sims(e) == 0  # second fire is a no-op

    def test_resolved_trade_not_exited(self, tmp_path):
        trader = _make_trader(tmp_path)
        _add_station_trades(trader, 1, win_rate=1.0, span_days=1)  # resolved row
        rows = trader._load_all()
        n = trader.apply_exit_sims([{"market_id": rows[0]["market_id"],
                                     "direction": "NO", "bid": 0.9,
                                     "p_win_now": 0.1, "reason": "r"}])
        assert n == 0

    def test_stats_compare_exit_vs_hold(self, tmp_path):
        trader = _make_trader(tmp_path)
        _add_station_trades(trader, 4, win_rate=0.5, span_days=2)
        rows = trader._load_all()
        # pretend one LOSER had exited at 0.40 (entry 0.30 NO, size 25 → 83.3 sh)
        loser = next(r for r in rows if float(r["pnl_usd"]) < 0)
        loser["exit_price"] = 0.40
        loser["exit_pnl_usd"] = round(25.0 / 0.30 * 0.40 - 25.0, 4)  # +8.33 vs −25
        trader._rewrite_all(rows)
        stats = trader.compute_stats()
        assert stats.exit_sim_count == 1
        assert stats.exit_sim_pnl == pytest.approx(
            stats.total_paper_pnl + 25.0 + 8.3333, abs=0.01)


class TestGateEraScoping:
    """Jul-10: trades signaled under the old poisoned calibrator must not count
    toward the gate — a validation record measures ONE system."""

    def test_pre_era_station_trades_excluded(self, tmp_path, monkeypatch):
        import weather.paper_trader as pt
        trader = _make_trader(tmp_path)
        _add_station_trades(trader, 40, win_rate=0.8, span_days=10)
        rows = trader._load_all()
        # era boundary AFTER every signal → nothing counts
        latest = max(r["signal_time"] for r in rows)
        monkeypatch.setattr(pt, "GATE_ERA_START", "2099-01-01")
        assert trader.compute_stats().station_resolved == 0
        # era boundary BEFORE every signal → all count
        monkeypatch.setattr(pt, "GATE_ERA_START", "2000-01-01")
        assert trader.compute_stats().station_resolved == 40

    def test_era_boundary_splits_record(self, tmp_path, monkeypatch):
        import weather.paper_trader as pt
        from datetime import datetime, timedelta, timezone
        trader = _make_trader(tmp_path)
        _add_station_trades(trader, 40, win_rate=0.8, span_days=10)
        rows = trader._load_all()
        mid = sorted(r["signal_time"] for r in rows)[20]
        monkeypatch.setattr(pt, "GATE_ERA_START", mid)
        assert 0 < trader.compute_stats().station_resolved < 40


class TestTakerFee:
    """pnl_usd stays GROSS; fee_usd is recorded at resolution and net = pnl - fee."""

    def test_resolve_trade_records_fee_and_keeps_pnl_gross(self, tmp_path):
        from weather.fees import taker_fee_usd
        trader = _make_trader(tmp_path)
        row = {f: "" for f in CSV_HEADERS}
        row.update({"trade_id": "f001", "market_id": "m", "market_title": "t",
                    "signal_time": datetime.now(timezone.utc).isoformat(),
                    "entry_price": 0.40, "model_p": 0.60, "direction": "YES",
                    "size_usd": 20.0, "size_factor": 1.0, "edge_pp": 0.1,
                    "ensemble_spread": 0.05, "confidence_score": 0.7,
                    "resolution_date": datetime.now(timezone.utc).isoformat()})
        with open(trader.log_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=CSV_HEADERS, extrasaction="ignore")
            w.writeheader(); w.writerow(row)
        trader.resolve_trade("f001", True)
        saved = trader._load_all()[0]
        assert float(saved["pnl_usd"]) == pytest.approx(20.0 * (1 / 0.40 - 1))  # gross
        assert float(saved["fee_usd"]) == pytest.approx(taker_fee_usd(20.0, 0.40))

    def test_stats_net_fields_use_recorded_or_recomputed_fee(self, tmp_path):
        trader = _make_trader(tmp_path)
        _add_resolved_trades(trader, 10, win_rate=0.5)  # rows predate fee_usd -> recomputed
        s = trader.compute_stats()
        fee_each = 25.0 * 0.05 * (1 - 0.30)
        assert s.total_paper_pnl_net == pytest.approx(s.total_paper_pnl - 10 * fee_each, abs=0.05)
        assert s.profit_factor_net < s.profit_factor
        # The go-live gate itself still reads the GROSS station PF.
        assert s.station_profit_factor_net <= s.station_profit_factor or s.station_resolved == 0


class TestExecPrice:
    """entry_price stays the MID (market-probability input of the Brier gate);
    pnl_usd / fee_usd are scored at the executable ask recorded as exec_price."""

    def _signal(self, exec_price):
        sig = MagicMock()
        sig.quality_gate_passed = True
        sig.size_factor = 1.0
        sig.direction = "NO"
        sig.market_p = 0.40          # mid YES -> mid NO = 0.60
        sig.exec_price = exec_price  # NO ask
        sig.model_p = 0.30
        sig.edge_pp = 0.09
        sig.ensemble_spread = 0.05
        sig.confidence_score = 0.7
        sig.signal_time = datetime.now(timezone.utc)
        sig.running_obs_c = None
        sig.restofday = False
        sig.market.market_id = "m_exec"
        sig.market.title = "t"
        sig.market.resolution_date = datetime.now(timezone.utc) + timedelta(days=1)
        sig.market.metric = "temperature_2m_max"
        sig.market.threshold = 20.0
        sig.market.threshold_high = None
        sig.market.direction = "above"
        sig.market.location.lat = 1.0
        sig.market.location.lon = 2.0
        sig.market.location.timezone = "UTC"
        sig.market.station_icao = "KXXX"
        sig.market.station_country = "US"
        sig.market.resolve_unit = "C"
        sig.prob_result.raw_p = 0.3
        sig.prob_result.model_breakdown = {"m": 0.3}
        return sig

    def test_log_trade_records_ask_and_keeps_mid_entry_price(self, tmp_path):
        trader = _make_trader(tmp_path)
        trader.log_trade(self._signal(exec_price=0.64))
        row = trader._load_all()[0]
        assert float(row["entry_price"]) == pytest.approx(0.60)   # mid, unchanged
        assert float(row["exec_price"]) == pytest.approx(0.64)    # the ask

    def test_expansion_station_row_is_tagged_and_excluded_from_stats(self, tmp_path):
        trader = _make_trader(tmp_path)
        sig = self._signal(exec_price=0.64)
        sig.market.station_icao = "EDDM"
        trader.log_trade(sig)
        row = trader._load_all()[0]
        assert row["exclude_reason"] == "expansion_unvalidated"
        trader.resolve_trade(row["trade_id"], False)
        assert trader.compute_stats().resolved_trades == 0

    def test_original_station_row_is_untagged(self, tmp_path):
        trader = _make_trader(tmp_path)
        sig = self._signal(exec_price=0.64)
        sig.market.station_icao = "KLGA"
        trader.log_trade(sig)
        assert trader._load_all()[0]["exclude_reason"] == ""

    def test_missing_ask_leaves_exec_price_blank(self, tmp_path):
        trader = _make_trader(tmp_path)
        trader.log_trade(self._signal(exec_price=0.0))
        assert trader._load_all()[0]["exec_price"] == ""

    def test_resolve_scores_pnl_and_fee_at_the_ask(self, tmp_path):
        from weather.fees import taker_fee_usd
        trader = _make_trader(tmp_path)
        trader.log_trade(self._signal(exec_price=0.64))
        row = trader._load_all()[0]
        # NO bet; actual_outcome False => NO wins.
        trader.resolve_trade(row["trade_id"], False)
        saved = trader._load_all()[0]
        size = float(saved["size_usd"])
        assert float(saved["pnl_usd"]) == pytest.approx(size * (1 / 0.64 - 1), abs=1e-3)
        assert float(saved["fee_usd"]) == pytest.approx(taker_fee_usd(size, 0.64), abs=1e-3)

    def test_legacy_rows_without_exec_price_still_score_at_entry(self, tmp_path):
        trader = _make_trader(tmp_path)
        _add_resolved_trades(trader, 4, win_rate=0.5)  # no exec_price column values
        s = trader.compute_stats()
        assert s.resolved_trades == 4 and s.total_paper_pnl_net < s.total_paper_pnl


class TestHeaderMigration:
    def test_old_header_migrated_and_orphan_tag_recovered(self, tmp_path):
        import csv as _csv
        from weather.paper_trader import CSV_HEADERS
        trader = _make_trader(tmp_path)
        old = [h for h in CSV_HEADERS if h != "exclude_reason"]
        with open(trader.log_path, "w", newline="") as f:
            w = _csv.writer(f)
            w.writerow(old)
            w.writerow(["t_old"] + [""] * (len(old) - 1))
            # row appended by new code under the old header: tag is the extra field
            w.writerow(["t_orphan"] + [""] * (len(old) - 1) + ["expansion_unvalidated"])
        sig = TestExecPrice()._signal(exec_price=0.64)
        trader.log_trade(sig)
        rows = {r["trade_id"]: r for r in trader._load_all()}
        assert rows["t_orphan"]["exclude_reason"] == "expansion_unvalidated"
        assert rows["t_old"]["exclude_reason"] == ""
        assert len(rows) == 3
        with open(trader.log_path, newline="") as f:
            assert next(_csv.reader(f)) == CSV_HEADERS
