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
