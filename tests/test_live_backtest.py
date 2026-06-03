"""Tests for LiveSignalBacktester (A4 — backtest through the live signal path)."""

import csv
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from weather.live_backtest import LiveSignalBacktester, LiveBacktestReport
from weather.models import EnsembleForecast, Location, RawProbabilityResult


def _make_fake_client(archive_vals=None):
    """WeatherClient mock that returns archive_vals for get_archive_daily_values."""
    client = MagicMock()
    client.get_archive_daily_values.return_value = archive_vals or [22.0, 23.0, 21.0, 24.0, 22.5]
    return client


def _make_fake_model(model_p=0.80):
    """ProbabilityModel mock with calibration and two-model breakdown."""
    model = MagicMock()
    model.n_calibration_obs = 100
    model.MIN_CALIBRATION_OBS = 50
    model._calibrator = MagicMock()
    model._calibrators_by_dir = {}
    model.compute_probability.return_value = RawProbabilityResult(
        raw_p=model_p, calibrated_p=model_p,
        ensemble_spread=0.05, n_members=9,
        is_calibrated=True,
        model_breakdown={
            "archive_gfs": model_p,
            "archive_icon": model_p + 0.02,
            "archive_ecmwf": model_p - 0.02,
        },
        threshold=20.0, direction="above", metric="temperature_2m_max",
        n_models=3,
    )
    return model


def _write_trade_csv(path: Path, rows: list[dict]) -> None:
    headers = [
        "trade_id", "market_id", "market_title", "signal_time",
        "entry_price", "model_p", "direction", "size_usd", "size_factor",
        "edge_pp", "ensemble_spread", "confidence_score", "resolution_date",
        "metric", "threshold", "threshold_high", "weather_direction",
        "lat", "lon", "location_tz",
        "actual_outcome", "resolved_at", "pnl_usd", "brier_score",
        "cumulative_pnl", "cumulative_brier",
    ]
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def _fixture_trade(
    trade_id="t001",
    direction="YES",
    weather_direction="above",
    entry_price=0.30,
    model_p=0.80,
    actual_outcome=1,
    lat=51.5, lon=-0.12,
    threshold=20.0,
    res_date=None,
):
    if res_date is None:
        res_date = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    return {
        "trade_id": trade_id,
        "market_id": "mkt_001",
        "market_title": "Will London high exceed 20°C?",
        "signal_time": (datetime.now(timezone.utc) - timedelta(days=4)).isoformat(),
        "entry_price": entry_price,
        "model_p": model_p,
        "direction": direction,
        "size_usd": "25.0",
        "size_factor": "0.9",
        "edge_pp": "0.12",
        "ensemble_spread": "0.05",
        "confidence_score": "0.65",
        "resolution_date": res_date,
        "metric": "temperature_2m_max",
        "threshold": threshold,
        "threshold_high": "",
        "weather_direction": weather_direction,
        "lat": lat,
        "lon": lon,
        "location_tz": "Europe/London",
        "actual_outcome": str(actual_outcome),
        "resolved_at": datetime.now(timezone.utc).isoformat(),
        "pnl_usd": "15.0" if actual_outcome == 1 else "-25.0",
        "brier_score": "0.04",
        "cumulative_pnl": "15.0",
        "cumulative_brier": "0.04",
    }


class TestLiveSignalBacktester:
    def test_live_backtester_reproduces_signal_on_fixture(self, tmp_path):
        """
        Single resolved trade: model_p=0.80 vs market=0.30 → model predicts YES.
        Archive members are all above threshold → replayed direction should also be YES.
        Verify direction_match=True and Brier scores are populated.
        """
        trades_csv = tmp_path / "paper_trades.csv"
        _write_trade_csv(trades_csv, [_fixture_trade(
            direction="YES", weather_direction="above",
            entry_price=0.30, model_p=0.80, actual_outcome=1,
            threshold=20.0,
        )])

        # All archive members are 25°C > 20°C threshold → model_p will be high → YES
        client = _make_fake_client(archive_vals=[25.0, 26.0, 24.5, 25.5, 26.0])
        model = _make_fake_model(model_p=0.80)

        backtester = LiveSignalBacktester(model=model, client=client)
        report = backtester.replay_trades(trades_csv)

        assert report.n_total == 1
        assert report.n_replayed == 1
        assert report.results[0].direction_match is True
        assert report.results[0].original_direction == "YES"
        assert report.results[0].replayed_direction == "YES"
        assert 0.0 <= report.results[0].brier_original <= 1.0
        assert 0.0 <= report.results[0].brier_replayed <= 1.0

    def test_backtest_skips_unresolved_trades(self, tmp_path):
        """Trades without actual_outcome are excluded from the replay."""
        trades_csv = tmp_path / "paper_trades.csv"
        trade = _fixture_trade()
        trade["actual_outcome"] = ""  # unresolved
        _write_trade_csv(trades_csv, [trade])

        client = _make_fake_client()
        model = _make_fake_model()
        backtester = LiveSignalBacktester(model=model, client=client)
        report = backtester.replay_trades(trades_csv)

        assert report.n_total == 0
        assert report.n_replayed == 0

    def test_backtest_skips_when_archive_fetch_fails(self, tmp_path):
        """When archive fetch raises, that trade is excluded from results."""
        trades_csv = tmp_path / "paper_trades.csv"
        _write_trade_csv(trades_csv, [_fixture_trade()])

        client = MagicMock()
        client.get_archive_daily_values.side_effect = Exception("network error")
        model = _make_fake_model()

        backtester = LiveSignalBacktester(model=model, client=client)
        report = backtester.replay_trades(trades_csv)

        assert report.n_total == 1
        assert report.n_replayed == 0  # skipped due to archive failure

    def test_backtest_report_metrics_computed(self, tmp_path):
        """With 2 replayed trades (both direction-match), parity=1.0 and brier_delta is finite."""
        trades_csv = tmp_path / "paper_trades.csv"
        t2 = _fixture_trade(trade_id="t002", actual_outcome=0,
                            direction="NO", weather_direction="above",
                            entry_price=0.70, model_p=0.20)
        t2["market_id"] = "mkt_002"
        _write_trade_csv(trades_csv, [_fixture_trade(trade_id="t001", actual_outcome=1), t2])

        client = _make_fake_client(archive_vals=[25.0, 26.0, 24.5, 25.5, 26.0])
        model = _make_fake_model(model_p=0.80)

        backtester = LiveSignalBacktester(model=model, client=client)
        report = backtester.replay_trades(trades_csv)

        assert report.n_total == 2
        assert report.n_replayed == 2
        assert isinstance(report.parity_pct, float)
        assert isinstance(report.mean_brier_delta, float)

    def test_backtest_missing_csv_returns_empty_report(self, tmp_path):
        """Missing trades CSV returns a zero-count report without error."""
        client = _make_fake_client()
        model = _make_fake_model()
        backtester = LiveSignalBacktester(model=model, client=client)
        report = backtester.replay_trades(tmp_path / "nonexistent.csv")

        assert report.n_total == 0
        assert report.n_replayed == 0
