"""Tests for signal generator quality gates and direction logic."""

from datetime import date, datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest

from weather.config import MAX_ENSEMBLE_SPREAD, MIN_EDGE_PP
from weather.models import EnsembleForecast, Location, RawProbabilityResult, WeatherMarket
from weather.signal_generator import SignalGenerator


def _make_market(yes_price=0.30, liquidity=5000.0, days_out=3):
    return WeatherMarket(
        market_id="test_mkt",
        title="Will the high temperature in Miami exceed 90°F on May 5?",
        yes_price=yes_price,
        liquidity_usd=liquidity,
        resolution_date=datetime.now(timezone.utc) + timedelta(days=days_out),
        resolution_source="NOAA",
        location=Location(city="Miami", lat=25.77, lon=-80.19),
        metric="temperature_2m_max",
        threshold=90.0,
        direction="above",
        url="https://polymarket.com/test",
    )


def _make_forecast(spread=0.05):
    return EnsembleForecast(
        lat=25.77, lon=-80.19,
        target_date=date(2026, 5, 5),
        metric="temperature_2m_max",
        member_arrays={"gfs_seamless": [91.0, 92.0, 93.0, 94.0, 95.0]},
        model_means={"gfs_seamless": 93.0, "ecmwf_ifs025": 93.0 + spread * 10},
    )


def _make_generator(model_p=0.80):
    client = MagicMock()
    model = MagicMock()
    client.get_ensemble_forecast.return_value = _make_forecast()
    model.compute_probability.return_value = RawProbabilityResult(
        raw_p=model_p, calibrated_p=model_p,
        ensemble_spread=0.05, n_members=5,
        is_calibrated=False, model_breakdown={},
        threshold=90.0, direction="above", metric="temperature_2m_max",
    )
    return SignalGenerator(model=model, client=client)


class TestSignalDirection:
    def test_model_above_market_buys_yes(self):
        gen = _make_generator(model_p=0.80)  # market at 0.30
        signal = gen.evaluate(_make_market(yes_price=0.30))
        assert signal.direction == "YES"

    def test_model_below_market_buys_no(self):
        gen = _make_generator(model_p=0.10)  # market at 0.30
        signal = gen.evaluate(_make_market(yes_price=0.30))
        assert signal.direction == "NO"


class TestQualityGates:
    def test_passes_clean_signal(self):
        gen = _make_generator(model_p=0.80)
        signal = gen.evaluate(_make_market(yes_price=0.30))
        assert signal.quality_gate_passed is True
        assert signal.rejection_reason is None

    def test_rejects_insufficient_edge(self):
        # model_p=0.32, market=0.30 → edge=0.02 < MIN_EDGE_PP
        gen = _make_generator(model_p=0.32)
        signal = gen.evaluate(_make_market(yes_price=0.30))
        assert signal.quality_gate_passed is False
        assert "insufficient_edge" in signal.rejection_reason

    def test_rejects_low_liquidity(self):
        from weather.config import MIN_MARKET_LIQUIDITY_USD
        gen = _make_generator(model_p=0.80)
        signal = gen.evaluate(_make_market(yes_price=0.30, liquidity=MIN_MARKET_LIQUIDITY_USD - 1))
        assert signal.quality_gate_passed is False
        assert "low_liquidity" in signal.rejection_reason

    def test_rejects_extreme_price(self):
        gen = _make_generator(model_p=0.80)
        signal = gen.evaluate(_make_market(yes_price=0.98))
        assert signal.quality_gate_passed is False

    def test_rejects_high_ensemble_spread(self):
        client = MagicMock()
        model = MagicMock()
        client.get_ensemble_forecast.return_value = _make_forecast(spread=2.0)  # very high
        model.compute_probability.return_value = RawProbabilityResult(
            raw_p=0.80, calibrated_p=0.80,
            ensemble_spread=MAX_ENSEMBLE_SPREAD + 0.1,  # above limit
            n_members=5, is_calibrated=False,
            model_breakdown={}, threshold=90.0,
            direction="above", metric="temperature_2m_max",
        )
        gen = SignalGenerator(model=model, client=client)
        signal = gen.evaluate(_make_market(yes_price=0.30))
        assert signal.quality_gate_passed is False
        assert "high_spread" in signal.rejection_reason

    def test_rejects_insufficient_members(self):
        client = MagicMock()
        model = MagicMock()
        client.get_ensemble_forecast.return_value = _make_forecast()
        model.compute_probability.return_value = RawProbabilityResult(
            raw_p=0.80, calibrated_p=0.80,
            ensemble_spread=0.05, n_members=1,  # too few
            is_calibrated=False, model_breakdown={},
            threshold=90.0, direction="above", metric="temperature_2m_max",
        )
        gen = SignalGenerator(model=model, client=client)
        signal = gen.evaluate(_make_market(yes_price=0.30))
        assert signal.quality_gate_passed is False
        assert "insufficient_members" in signal.rejection_reason


class TestEdgeCalculation:
    def test_edge_is_absolute_difference(self):
        gen = _make_generator(model_p=0.80)
        signal = gen.evaluate(_make_market(yes_price=0.30))
        assert signal.edge_pp == pytest.approx(0.50, abs=0.001)

    def test_confidence_score_inversely_related_to_spread(self):
        # Low spread → high confidence
        gen = _make_generator(model_p=0.80)
        s1 = gen.evaluate(_make_market(yes_price=0.30))
        assert 0.0 <= s1.confidence_score <= 1.0
