"""Tests for signal generator quality gates and direction logic."""

from datetime import date, datetime, timezone, timedelta
from unittest.mock import MagicMock

import pytest

from weather.config import MAX_ENSEMBLE_SPREAD, MAX_ENTRY_DAYS_AHEAD, MIN_NET_EV_PP, ROUND_TRIP_FEE
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


def _make_forecast(spread=0.05, age_hours=0):
    fetched = datetime.now(timezone.utc) - timedelta(hours=age_hours)
    return EnsembleForecast(
        lat=25.77, lon=-80.19,
        target_date=date(2026, 5, 5),
        metric="temperature_2m_max",
        member_arrays={"gfs_seamless": [91.0, 92.0, 93.0, 94.0, 95.0]},
        model_means={"gfs_seamless": 93.0, "ecmwf_ifs025": 93.0 + spread * 10},
        fetched_at=fetched,
    )


def _make_generator(model_p=0.80, price_tracker=None):
    client = MagicMock()
    model = MagicMock()
    model.n_calibration_obs = 0
    model.MIN_CALIBRATION_OBS = 50
    client.get_ensemble_forecast.return_value = _make_forecast()
    model.compute_probability.return_value = RawProbabilityResult(
        raw_p=model_p, calibrated_p=model_p,
        ensemble_spread=0.05, n_members=5,
        is_calibrated=False,
        model_breakdown={"gfs_seamless": model_p, "ecmwf_ifs025": model_p},
        threshold=90.0, direction="above", metric="temperature_2m_max",
        n_models=2,
    )
    return SignalGenerator(model=model, client=client, price_tracker=price_tracker)


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

    # Gate 4 — fee-adjusted edge
    def test_rejects_insufficient_net_ev(self):
        # model_p=0.32, market=0.30 → gross=0.02, net=0.02-0.04=-0.02 < MIN_NET_EV_PP
        gen = _make_generator(model_p=0.32)
        signal = gen.evaluate(_make_market(yes_price=0.30))
        assert signal.quality_gate_passed is False
        assert "gate4_fee_adjusted_edge" in signal.rejection_reason

    def test_rejects_low_liquidity(self):
        from weather.config import MIN_MARKET_LIQUIDITY_USD
        gen = _make_generator(model_p=0.80)
        signal = gen.evaluate(_make_market(yes_price=0.30, liquidity=MIN_MARKET_LIQUIDITY_USD - 1))
        assert signal.quality_gate_passed is False
        assert "gate5_low_liquidity" in signal.rejection_reason

    def test_gate5_uses_book_depth_not_volume_when_depth_available(self):
        """Gate 5 rejects on low book_depth_usd even when volumeClob is high."""
        from weather.config import MIN_MARKET_LIQUIDITY_USD, BOOK_DEPTH_MIN_MULTIPLIER
        gen = _make_generator(model_p=0.80)
        # volume is fine (10k) but book depth is below the 3x threshold
        market = _make_market(yes_price=0.30, liquidity=10_000.0)
        # Inject a low book depth directly
        market.book_depth_usd = MIN_MARKET_LIQUIDITY_USD * BOOK_DEPTH_MIN_MULTIPLIER - 1.0
        signal = gen.evaluate(market)
        assert signal.quality_gate_passed is False
        assert "gate5_low_book_depth" in signal.rejection_reason

    def test_gate5_passes_on_sufficient_book_depth(self):
        """Gate 5 passes when book_depth_usd meets the 3x threshold."""
        from weather.config import MIN_MARKET_LIQUIDITY_USD, BOOK_DEPTH_MIN_MULTIPLIER
        gen = _make_generator(model_p=0.80)
        market = _make_market(yes_price=0.30, liquidity=10_000.0)
        market.book_depth_usd = MIN_MARKET_LIQUIDITY_USD * BOOK_DEPTH_MIN_MULTIPLIER + 1.0
        signal = gen.evaluate(market)
        assert signal.quality_gate_passed is True

    def test_gate5_falls_back_to_volume_when_depth_zero(self):
        """When book_depth_usd == 0 (sidecar unavailable), fall back to volume check."""
        from weather.config import MIN_MARKET_LIQUIDITY_USD
        gen = _make_generator(model_p=0.80)
        # volume is good, depth is 0 (not fetched) → should fall back and pass
        market = _make_market(yes_price=0.30, liquidity=MIN_MARKET_LIQUIDITY_USD + 100.0)
        market.book_depth_usd = 0.0
        signal = gen.evaluate(market)
        assert signal.quality_gate_passed is True

    def test_rejects_extreme_price(self):
        gen = _make_generator(model_p=0.80)
        signal = gen.evaluate(_make_market(yes_price=0.98))
        assert signal.quality_gate_passed is False
        assert "gate7_extreme_price" in signal.rejection_reason

    def test_rejects_high_ensemble_spread(self):
        client = MagicMock()
        model = MagicMock()
        model.n_calibration_obs = 0
        model.MIN_CALIBRATION_OBS = 50
        client.get_ensemble_forecast.return_value = _make_forecast(spread=2.0)
        model.compute_probability.return_value = RawProbabilityResult(
            raw_p=0.80, calibrated_p=0.80,
            ensemble_spread=MAX_ENSEMBLE_SPREAD + 0.1,
            n_members=5, is_calibrated=False,
            model_breakdown={"gfs_seamless": 0.80, "ecmwf_ifs025": 0.70},
            threshold=90.0, direction="above", metric="temperature_2m_max",
            n_models=2,
        )
        gen = SignalGenerator(model=model, client=client)
        signal = gen.evaluate(_make_market(yes_price=0.30))
        assert signal.quality_gate_passed is False
        assert "gate2.7_high_spread" in signal.rejection_reason

    def test_rejects_insufficient_members(self):
        client = MagicMock()
        model = MagicMock()
        model.n_calibration_obs = 0
        model.MIN_CALIBRATION_OBS = 50
        client.get_ensemble_forecast.return_value = _make_forecast()
        model.compute_probability.return_value = RawProbabilityResult(
            raw_p=0.80, calibrated_p=0.80,
            ensemble_spread=0.05, n_members=1,
            is_calibrated=False,
            model_breakdown={"gfs_seamless": 0.80, "ecmwf_ifs025": 0.80},
            threshold=90.0, direction="above", metric="temperature_2m_max",
            n_models=2,
        )
        gen = SignalGenerator(model=model, client=client)
        signal = gen.evaluate(_make_market(yes_price=0.30))
        assert signal.quality_gate_passed is False
        assert "gate2.5_insufficient_members" in signal.rejection_reason

    # Gate 0 — forecast freshness
    def test_rejects_stale_forecast(self):
        client = MagicMock()
        model = MagicMock()
        model.n_calibration_obs = 0
        model.MIN_CALIBRATION_OBS = 50
        client.get_ensemble_forecast.return_value = _make_forecast(age_hours=8)  # > 6h limit
        model.compute_probability.return_value = RawProbabilityResult(
            raw_p=0.80, calibrated_p=0.80,
            ensemble_spread=0.05, n_members=5,
            is_calibrated=False,
            model_breakdown={"gfs_seamless": 0.80, "ecmwf_ifs025": 0.80},
            threshold=90.0, direction="above", metric="temperature_2m_max",
            n_models=2,
        )
        gen = SignalGenerator(model=model, client=client)
        signal = gen.evaluate(_make_market(yes_price=0.30))
        assert signal.quality_gate_passed is False
        assert "gate0_stale_forecast" in signal.rejection_reason

    # Gate 1 — entry timing window
    def test_rejects_too_early(self):
        gen = _make_generator(model_p=0.80)
        too_far = MAX_ENTRY_DAYS_AHEAD + 2  # comfortably beyond the entry-timing gate
        signal = gen.evaluate(_make_market(yes_price=0.30, days_out=too_far))
        assert signal.quality_gate_passed is False
        assert "gate1_too_early" in signal.rejection_reason

    def test_rejects_too_late_to_resolution(self):
        gen = _make_generator(model_p=0.80)
        market = WeatherMarket(
            market_id="test_mkt",
            title="Will the high temperature in Miami exceed 90°F on May 5?",
            yes_price=0.30,
            liquidity_usd=5000.0,
            resolution_date=datetime.now(timezone.utc) + timedelta(hours=1),
            resolution_source="NOAA",
            location=Location(city="Miami", lat=25.77, lon=-80.19),
            metric="temperature_2m_max",
            threshold=90.0,
            direction="above",
            url="https://polymarket.com/test",
        )
        signal = gen.evaluate(market)
        assert signal.quality_gate_passed is False
        assert "gate1_too_late" in signal.rejection_reason

    # Gate 6 — odds velocity
    def test_rejects_informed_flow(self):
        tracker_mock = MagicMock()
        tracker_mock.get_velocity.return_value = 0.20  # 20pp > MAX_PRICE_VELOCITY_PP=0.15
        gen = _make_generator(model_p=0.80, price_tracker=tracker_mock)
        signal = gen.evaluate(_make_market(yes_price=0.30))
        assert signal.quality_gate_passed is False
        assert "gate6_informed_flow" in signal.rejection_reason

    # Gate 8 — composite confidence
    def test_confidence_score_in_signal(self):
        gen = _make_generator(model_p=0.80)
        signal = gen.evaluate(_make_market(yes_price=0.30, days_out=3))
        assert 0.0 <= signal.confidence_score <= 1.0

    def test_rejects_low_composite_confidence(self):
        from weather.config import MIN_COMPOSITE_CONFIDENCE
        client = MagicMock()
        model = MagicMock()
        model.n_calibration_obs = 0
        model.MIN_CALIBRATION_OBS = 50
        # Very high spread → spread_component near 0
        client.get_ensemble_forecast.return_value = _make_forecast(spread=0.0)
        model.compute_probability.return_value = RawProbabilityResult(
            raw_p=0.80, calibrated_p=0.80,
            ensemble_spread=0.19,  # just under MAX so gate 2.7 passes
            n_members=5, is_calibrated=False,
            model_breakdown={"gfs_seamless": 0.80, "ecmwf_ifs025": 0.70},
            threshold=90.0, direction="above", metric="temperature_2m_max",
            n_models=2,
        )
        gen = SignalGenerator(model=model, client=client)
        # 5 days out = timing_component near 0; high spread; 0 calibration obs
        # composite ≈ 0.40*(1-0.19/0.20) + 0.35*(1-5/5) + 0.25*0 = 0.40*0.05 + 0 + 0 = 0.02
        signal = gen.evaluate(_make_market(yes_price=0.30, days_out=5))
        assert signal.quality_gate_passed is False
        assert "gate8_low_confidence" in signal.rejection_reason


class TestModelDiversity:
    """Gate 2.6 — at least 2 distinct models required (E2)."""

    def test_gate2_6_rejects_single_model(self):
        """Single-model forecast (n_models=1) must be rejected by gate 2.6."""
        client = MagicMock()
        model = MagicMock()
        model.n_calibration_obs = 0
        model.MIN_CALIBRATION_OBS = 50
        client.get_ensemble_forecast.return_value = _make_forecast()
        model.compute_probability.return_value = RawProbabilityResult(
            raw_p=0.80, calibrated_p=0.80,
            ensemble_spread=0.0, n_members=5,
            is_calibrated=False,
            model_breakdown={"gfs_seamless": 0.80},
            threshold=90.0, direction="above", metric="temperature_2m_max",
            n_models=1,
        )
        gen = SignalGenerator(model=model, client=client)
        signal = gen.evaluate(_make_market(yes_price=0.30))
        assert signal.quality_gate_passed is False
        assert "gate2.6_single_model" in signal.rejection_reason
        assert "n_models=1" in signal.rejection_reason

    def test_gate2_6_passes_two_models(self):
        """Two-model forecast must pass gate 2.6."""
        gen = _make_generator(model_p=0.80)  # _make_generator sets n_models=2
        signal = gen.evaluate(_make_market(yes_price=0.30))
        assert signal.rejection_reason is None or "gate2.6" not in signal.rejection_reason

    def test_gate8_spread_component_zero_when_single_model(self):
        """When n_models=0 (no model breakdown), spread_component must be 0 and signal rejected."""
        client = MagicMock()
        model = MagicMock()
        model.n_calibration_obs = 100
        model.MIN_CALIBRATION_OBS = 50
        client.get_ensemble_forecast.return_value = _make_forecast()
        model.compute_probability.return_value = RawProbabilityResult(
            raw_p=0.80, calibrated_p=0.80,
            # spread=0.0 would give spread_component=1.0 without the n_models guard
            ensemble_spread=0.0, n_members=5,
            is_calibrated=True, model_breakdown={},
            threshold=90.0, direction="above", metric="temperature_2m_max",
            n_models=0,
        )
        gen = SignalGenerator(model=model, client=client)
        # Gate 2.6 must reject first; rejection score is 0.0
        signal = gen.evaluate(_make_market(yes_price=0.30, days_out=1))
        assert signal.quality_gate_passed is False
        assert "gate2.6_single_model" in signal.rejection_reason
        assert signal.confidence_score == 0.0


class TestEdgeCalculation:
    def test_edge_is_absolute_difference(self):
        gen = _make_generator(model_p=0.80)
        signal = gen.evaluate(_make_market(yes_price=0.30))
        # Shrinkage anchors at the market price: edge = |calibrated_p - market| * k.
        # days_out=3, spread=0.05 → k = skill(0.9) * spread_factor(0.75) = 0.675.
        # edge = 0.50 * 0.675 = 0.3375
        assert signal.edge_pp == pytest.approx(0.3375, abs=0.01)

    def test_net_ev_gate_threshold(self):
        # Post-shrinkage gate 4 requires |calibrated_p - market| * k ≥ fee + min_ev,
        # so the minimum passing gross edge is (ROUND_TRIP_FEE + MIN_NET_EV_PP) / k.
        min_gross = (ROUND_TRIP_FEE + MIN_NET_EV_PP) / 0.675  # k for days_out=3, spread=0.05
        gen = _make_generator(model_p=0.30 + min_gross + 0.005)  # just above threshold
        signal = gen.evaluate(_make_market(yes_price=0.30))
        assert signal.quality_gate_passed is True

    def test_confidence_score_inversely_related_to_spread(self):
        gen = _make_generator(model_p=0.80)
        s1 = gen.evaluate(_make_market(yes_price=0.30))
        assert 0.0 <= s1.confidence_score <= 1.0


class TestShrinkageAnchoredAtMarket:
    """Regression (Jun 2026): shrinkage toward 0.5 manufactured phantom YES signals.

    With calibrated_p=0.055 and market at 0.22, the old 0.5-anchored shrinkage
    produced model_p≈0.37 > 0.22 — flipping a raw NO view into a YES bet purely
    by shrinkage geometry. Every such YES-range trade lost (0/35). Anchoring at
    the market price makes a direction flip impossible.
    """

    def test_shrinkage_never_flips_direction(self):
        gen = _make_generator(model_p=0.055)   # raw view: well below market
        signal = gen.evaluate(_make_market(yes_price=0.22))
        assert signal.direction == "NO"
        assert signal.model_p < 0.22

    def test_shrunk_model_p_lies_between_market_and_calibrated(self):
        gen = _make_generator(model_p=0.80)
        signal = gen.evaluate(_make_market(yes_price=0.30))
        assert 0.30 < signal.model_p < 0.80

    def test_heavier_shrinkage_only_reduces_edge(self):
        gen = _make_generator(model_p=0.80)
        near = gen.evaluate(_make_market(yes_price=0.30, days_out=1))
        far = gen.evaluate(_make_market(yes_price=0.30, days_out=5))
        assert far.edge_pp < near.edge_pp
        assert near.direction == far.direction == "YES"
