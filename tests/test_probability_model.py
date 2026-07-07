"""Tests for the probability model — the most critical component."""

from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from weather.models import EnsembleForecast, Location
from weather.probability_model import ProbabilityModel, _fraction_satisfying


@pytest.fixture
def model(tmp_path):
    return ProbabilityModel(calibration_log_path=tmp_path / "calibration.csv")


@pytest.fixture
def forecast_above(target_date=None):
    """All members above threshold."""
    return EnsembleForecast(
        lat=25.77, lon=-80.19,
        target_date=date(2026, 5, 1),
        metric="temperature_2m_max",
        member_arrays={"gfs_seamless": [92.0, 93.0, 94.0, 91.0, 95.0]},
        model_means={"gfs_seamless": 93.0, "ecmwf_ifs025": 91.0},
    )


@pytest.fixture
def forecast_half():
    """Exactly half above threshold."""
    return EnsembleForecast(
        lat=25.77, lon=-80.19,
        target_date=date(2026, 5, 1),
        metric="temperature_2m_max",
        member_arrays={"gfs_seamless": [88.0, 89.0, 91.0, 92.0]},  # 2/4 above 90
        model_means={"gfs_seamless": 90.0, "ecmwf_ifs025": 90.5},
    )


@pytest.fixture
def forecast_none():
    """No members above threshold."""
    return EnsembleForecast(
        lat=25.77, lon=-80.19,
        target_date=date(2026, 5, 1),
        metric="temperature_2m_max",
        member_arrays={"gfs_seamless": [80.0, 81.0, 82.0, 83.0, 84.0]},
        model_means={"gfs_seamless": 82.0, "ecmwf_ifs025": 83.0},
    )


class TestFractionSatisfying:
    def test_all_above(self):
        assert _fraction_satisfying([92.0, 93.0, 94.0], 90.0, "above") == pytest.approx(1.0, abs=0.01)

    def test_none_above(self):
        assert _fraction_satisfying([80.0, 81.0, 82.0], 90.0, "above") == pytest.approx(0.0, abs=0.01)

    def test_half_above(self):
        result = _fraction_satisfying([88.0, 89.0, 91.0, 92.0], 90.0, "above")
        assert result == pytest.approx(0.5, abs=0.01)

    def test_direction_below(self):
        result = _fraction_satisfying([80.0, 81.0, 91.0, 92.0], 90.0, "below")
        assert result == pytest.approx(0.5, abs=0.01)

    def test_empty_members_returns_0_5(self):
        result = _fraction_satisfying([], 90.0, "above")
        assert result == pytest.approx(0.5)


class TestComputeProbability:
    def test_all_above_returns_high_p(self, model, forecast_above):
        result = model.compute_probability(forecast_above, threshold=90.0, direction="above")
        assert result.raw_p > 0.9

    def test_none_above_returns_low_p(self, model, forecast_none):
        result = model.compute_probability(forecast_none, threshold=90.0, direction="above")
        assert result.raw_p < 0.1

    def test_half_returns_near_0_5(self, model, forecast_half):
        result = model.compute_probability(forecast_half, threshold=90.0, direction="above")
        assert 0.3 < result.raw_p < 0.7

    def test_n_members_correct(self, model, forecast_above):
        result = model.compute_probability(forecast_above, threshold=90.0, direction="above")
        assert result.n_members == 5

    def test_uncalibrated_by_default(self, model, forecast_above):
        result = model.compute_probability(forecast_above, threshold=90.0, direction="above")
        assert result.is_calibrated is False
        assert result.calibrated_p == pytest.approx(result.raw_p, abs=0.001)

    def test_empty_forecast_returns_0_5(self, model):
        empty = EnsembleForecast(
            lat=0, lon=0, target_date=date(2026, 5, 1), metric="temperature_2m_max"
        )
        result = model.compute_probability(empty, threshold=90.0, direction="above")
        assert result.raw_p == pytest.approx(0.5)
        assert result.n_members == 0

    def test_ensemble_spread_uses_per_model_probabilities(self, model):
        # Two models that completely disagree on the threshold:
        # GFS all above (P=1.0), ICON all below (P=0.0). Spread = std([1.0, 0.0]) = 0.5.
        forecast = EnsembleForecast(
            lat=25.77, lon=-80.19,
            target_date=date(2026, 5, 1),
            metric="temperature_2m_max",
            member_arrays={
                "gfs_seamless": [92.0, 93.0, 94.0, 95.0, 96.0],
                "icon_seamless": [80.0, 81.0, 82.0, 83.0, 84.0],
            },
        )
        result = model.compute_probability(forecast, threshold=90.0, direction="above")
        assert result.ensemble_spread == pytest.approx(0.5, abs=0.01)


class TestSingleModelDegradation:
    """E2: single-model forecast should set n_models=1 and spread=0."""

    def test_compute_probability_single_model_flags_degraded(self, model):
        """When only one model has members, n_models=1 and spread=0.0."""
        single_model_forecast = EnsembleForecast(
            lat=25.77, lon=-80.19,
            target_date=date(2026, 5, 1),
            metric="temperature_2m_max",
            member_arrays={"gfs_seamless": [92.0, 93.0, 94.0, 95.0, 96.0]},
        )
        result = model.compute_probability(single_model_forecast, threshold=90.0, direction="above")
        assert result.n_models == 1
        assert result.ensemble_spread == pytest.approx(0.0)

    def test_compute_probability_two_models_sets_n_models_2(self, model):
        """With two models, n_models=2 and spread is non-zero when models disagree."""
        two_model_forecast = EnsembleForecast(
            lat=25.77, lon=-80.19,
            target_date=date(2026, 5, 1),
            metric="temperature_2m_max",
            member_arrays={
                "gfs_seamless": [92.0, 93.0, 94.0, 95.0],
                "icon_seamless": [80.0, 81.0, 82.0, 83.0],
            },
        )
        result = model.compute_probability(two_model_forecast, threshold=90.0, direction="above")
        assert result.n_models == 2
        assert result.ensemble_spread > 0.0


class TestEnsembleSpread:
    def test_single_model_spread_is_zero(self):
        f = EnsembleForecast(
            lat=0, lon=0, target_date=date(2026, 5, 1), metric="temperature_2m_max",
            model_means={"gfs_seamless": 90.0},
        )
        assert f.ensemble_std == 0.0

    def test_high_spread_computed_correctly(self):
        f = EnsembleForecast(
            lat=0, lon=0, target_date=date(2026, 5, 1), metric="temperature_2m_max",
            model_means={"gfs_seamless": 70.0, "ecmwf_ifs025": 90.0},
        )
        assert f.ensemble_std == pytest.approx(10.0, abs=0.01)


class TestCalibration:
    def test_calibration_not_active_with_few_obs(self, model):
        for i in range(10):
            model.log_observation(float(i) / 10, bool(i > 5))
        assert model._calibrator is None

    def test_calibration_activates_at_50_obs(self, model):
        for i in range(50):
            model.log_observation(float(i) / 50, i > 25)
        assert model._calibrator is not None

    def test_calibration_output_in_valid_range(self, model):
        for i in range(50):
            model.log_observation(float(i) / 50, i > 25)
        result = model._apply_calibration(0.6)
        assert 0.0 < result < 1.0

    def test_calibration_log_written(self, tmp_path, model):
        model.log_observation(0.7, True)
        assert model.calibration_log_path.exists()


class TestRoundingPreimage:
    """The market settles on whole-degree ROUNDED station values; the model must
    price the rounding pre-image of each bucket, not its exact edges."""

    def _forecast(self, members):
        return EnsembleForecast(
            lat=40.78, lon=-73.88,
            target_date=date(2026, 7, 8),
            metric="temperature_2m_max",
            member_arrays={"gfs_seamless": members},
            model_means={"gfs_seamless": float(np.mean(members)), "ecmwf_ifs025": float(np.mean(members))},
        )

    def test_helper_widens_f_range(self):
        from weather.probability_model import _rounding_preimage
        half_f = 0.5 * 5.0 / 9.0
        # "between 96–97°F" stored as °C edges → pre-image [95.5, 97.5)°F
        lo_c, hi_c = (96 - 32) * 5 / 9, (97 - 32) * 5 / 9
        d, t, th = _rounding_preimage(lo_c, "range", hi_c, "F", "temperature_2m_max")
        assert d == "range"
        assert t == pytest.approx(lo_c - half_f)
        assert th == pytest.approx(hi_c + half_f)

    def test_helper_equal_f_becomes_halfdegree_range(self):
        from weather.probability_model import _rounding_preimage
        half_f = 0.5 * 5.0 / 9.0
        t_c = (88 - 32) * 5 / 9
        d, t, th = _rounding_preimage(t_c, "equal", None, "F", "temperature_2m_max")
        # ±0.5°F ≈ ±0.28°C — NOT the legacy ±0.5°C (which was 1.8× too wide for °F)
        assert d == "range"
        assert th - t == pytest.approx(2 * half_f)

    def test_helper_above_below_inclusive_wing(self):
        from weather.probability_model import _rounding_preimage
        d, t, _ = _rounding_preimage(31.0, "above", None, "C", "temperature_2m_max")
        assert d == "above" and t == pytest.approx(30.5)
        d, t, _ = _rounding_preimage(13.0, "below", None, "C", "temperature_2m_max")
        assert d == "below" and t == pytest.approx(13.5)

    def test_helper_noop_without_unit_or_for_precip(self):
        from weather.probability_model import _rounding_preimage
        assert _rounding_preimage(30.0, "range", 31.0, "", "temperature_2m_max") == ("range", 30.0, 31.0)
        assert _rounding_preimage(5.0, "above", None, "F", "precipitation_sum") == ("above", 5.0, None)

    def test_range_probability_wider_with_unit(self, model):
        # Uniform-ish members straddling a 1°F bucket: the pre-image (2°F wide)
        # must carry roughly twice the probability of the exact edges.
        members = list(np.linspace(30.0, 40.0, 200))  # °C, flat density
        fc = self._forecast(members)
        lo_c, hi_c = (95 - 32) * 5 / 9, (96 - 32) * 5 / 9  # 95–96°F ≈ 35.0–35.56°C
        p_legacy = model.compute_probability(fc, lo_c, "range", hi_c).raw_p
        p_preimage = model.compute_probability(fc, lo_c, "range", hi_c, resolve_unit="F").raw_p
        assert p_preimage > 1.5 * p_legacy

    def test_equal_f_probability_narrower_than_legacy(self, model):
        # Legacy "equal" used ±0.5°C for everything; a °F market's true band is
        # ±0.5°F ≈ ±0.28°C, so the °F-aware probability must be LOWER.
        members = list(np.linspace(25.0, 37.0, 200))
        fc = self._forecast(members)
        t_c = (88 - 32) * 5 / 9
        p_legacy = model.compute_probability(fc, t_c, "equal").raw_p
        p_preimage = model.compute_probability(fc, t_c, "equal", resolve_unit="F").raw_p
        assert p_preimage < p_legacy

    def test_equal_c_probability_matches_legacy_band(self, model):
        # °C equal markets: pre-image ±0.5°C == the legacy band → ~same probability.
        members = list(np.linspace(25.0, 37.0, 200))
        fc = self._forecast(members)
        p_legacy = model.compute_probability(fc, 31.0, "equal").raw_p
        p_preimage = model.compute_probability(fc, 31.0, "equal", resolve_unit="C").raw_p
        assert p_preimage == pytest.approx(p_legacy, abs=0.02)

    def test_direction_label_preserved_for_calibrator_routing(self, model):
        # equal→range conversion is internal math only; the result must still
        # report direction="equal" so per-direction calibrators stay consistent.
        members = list(np.linspace(25.0, 37.0, 50))
        fc = self._forecast(members)
        r = model.compute_probability(fc, 31.0, "equal", resolve_unit="F")
        assert r.direction == "equal"
        assert r.threshold == 31.0  # original edge, not the widened one
