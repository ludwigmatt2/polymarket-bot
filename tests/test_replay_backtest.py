"""Tests for the Phase 0.5 replay backtest harness — deterministic, no network."""

from datetime import datetime, timezone, timedelta

import pytest

import replay_backtest as rb
from replay_backtest import BacktestConfig, Split, run_backtest, production_predict


def _trade(weather_direction="above", direction="YES", threshold=25.0,
           threshold_high="", outcome=1, days=2, lat=35.68, lon=139.69):
    sig = datetime.now(timezone.utc) - timedelta(days=days)
    res = sig + timedelta(days=days)
    return {
        "weather_direction": weather_direction, "direction": direction,
        "threshold": str(threshold), "threshold_high": str(threshold_high) if threshold_high != "" else "",
        "actual_outcome": str(outcome), "metric": "temperature_2m_max",
        "lat": str(lat), "lon": str(lon), "location_tz": "UTC",
        "signal_time": sig.isoformat(), "resolution_date": res.isoformat(),
        "entry_price": "0.30",   # side price; _market_yes_price converts NO → 1-entry
    }


class TestScoring:
    def test_split_metrics(self):
        s = Split()
        s.add(0.8, 1)   # brier .04, correct
        s.add(0.2, 0)   # brier .04, correct
        s.add(0.9, 0)   # brier .81, wrong
        assert s.n == 3
        assert s.brier == pytest.approx((0.04 + 0.04 + 0.81) / 3)
        assert s.accuracy == pytest.approx(2 / 3)
        assert s.mean_p == pytest.approx((0.8 + 0.2 + 0.9) / 3)

    def test_run_backtest_splits_by_direction_and_side(self):
        trades = [
            _trade(weather_direction="above", direction="YES", outcome=1),
            _trade(weather_direction="range", direction="NO", outcome=0),
        ]
        # constant-0.7 predictor, no member dependency
        cfg = BacktestConfig("const", predict=lambda m, t: 0.7, needs_members=False)
        rep = run_backtest(cfg, trades)
        assert rep.n_scored == 2
        assert "above" in rep.by_direction and "range" in rep.by_direction
        assert "YES" in rep.by_side and "NO" in rep.by_side
        assert rep.by_direction["above"].mean_p == pytest.approx(0.7)

    def test_none_prediction_is_skipped(self):
        trades = [_trade(), _trade()]
        cfg = BacktestConfig("none", predict=lambda m, t: None, needs_members=False)
        rep = run_backtest(cfg, trades)
        assert rep.n_scored == 0


class TestProductionPredict:
    def test_above_high_members_gives_high_p(self):
        members = {"gfs_seamless": [30.0] * 20, "ecmwf_ifs025": [31.0] * 20}
        t = _trade(weather_direction="above", threshold=25.0, days=1)
        p = production_predict(members, t)
        assert p > 0.8  # all members clear the threshold

    def test_member_shift_lowers_above_probability(self):
        # realistic spread around the threshold so KDE is well-defined
        spread = [24.0, 24.5, 25.0, 25.5, 26.0, 26.5, 27.0, 27.5, 28.0, 28.5]
        members = {"gfs_seamless": spread * 2, "ecmwf_ifs025": spread * 2}
        t = _trade(weather_direction="above", threshold=25.0, days=1)
        p_base = production_predict(members, t)
        # model runs +3°C warm → shift members DOWN 3 → fewer clear the threshold
        p_shift = production_predict(members, t, member_shift=3.0)
        assert p_shift < p_base

    def test_bias_offset_shifts_threshold(self):
        members = {"gfs_seamless": [25.2] * 20, "ecmwf_ifs025": [25.2] * 20}
        t = _trade(weather_direction="above", threshold=25.0, days=1)
        p_base = production_predict(members, t)
        # positive offset lowers threshold → more members clear → higher p
        p_off = production_predict(members, t, bias_offset=1.0)
        assert p_off >= p_base

    def test_empty_members_returns_none(self):
        assert production_predict({}, _trade()) is None

    def test_weights_change_result(self):
        # gfs leans above (14/20), ecmwf leans below (9/20) — moderate breakdown
        # spread so lead-time/spread shrinkage doesn't collapse both to 0.5.
        gfs = [26.0] * 14 + [24.0] * 6
        ecmwf = [26.0] * 9 + [24.0] * 11
        members = {"gfs_seamless": gfs, "ecmwf_ifs025": ecmwf}
        t = _trade(weather_direction="above", threshold=25.0, days=1)
        p_equal = production_predict(members, t, weights={"gfs_seamless": 1.0, "ecmwf_ifs025": 1.0})
        p_ecmwf = production_predict(members, t, weights={"gfs_seamless": 1.0, "ecmwf_ifs025": 5.0})
        # up-weighting the cold-leaning model (ecmwf) lowers P(above)
        assert p_ecmwf < p_equal
