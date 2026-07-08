"""Tests for the running-extreme observation feature (Point 2, Jul-8 plan)."""
from datetime import date, datetime, timezone

import numpy as np
import pytest

from weather import iem_client, station_obs
from weather.models import EnsembleForecast
from weather.probability_model import ProbabilityModel
from pathlib import Path


class TestStationObs:
    def setup_method(self):
        station_obs._cache.clear()

    def test_converts_to_celsius(self, monkeypatch):
        monkeypatch.setattr(iem_client, "metar_peak", lambda i, d, k: 86.0)  # 30°C
        v = station_obs.running_extreme_c("KLGA", date(2026, 7, 8), "max")
        assert v == pytest.approx(30.0)

    def test_none_when_no_obs(self, monkeypatch):
        monkeypatch.setattr(iem_client, "metar_peak", lambda i, d, k: None)
        assert station_obs.running_extreme_c("KLGA", date(2026, 7, 8), "max") is None

    def test_fetch_failure_stands_down(self, monkeypatch):
        def boom(i, d, k):
            raise OSError("iem down")
        monkeypatch.setattr(iem_client, "metar_peak", boom)
        assert station_obs.running_extreme_c("KLGA", date(2026, 7, 8), "max") is None

    def test_cached_within_hour(self, monkeypatch):
        calls = []
        monkeypatch.setattr(iem_client, "metar_peak", lambda i, d, k: calls.append(1) or 86.0)
        station_obs.running_extreme_c("KLGA", date(2026, 7, 8), "max")
        station_obs.running_extreme_c("KLGA", date(2026, 7, 8), "max")
        assert len(calls) == 1
        # different kind → separate probe
        station_obs.running_extreme_c("KLGA", date(2026, 7, 8), "min")
        assert len(calls) == 2

    def test_is_event_day(self):
        # resolution deadline 2026-07-08T12:00Z, NYC: event day = Jul 8 local
        res = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
        on_day = datetime(2026, 7, 8, 15, 0, tzinfo=timezone.utc)   # Jul 8, 11:00 EDT
        day_before = datetime(2026, 7, 7, 15, 0, tzinfo=timezone.utc)
        assert station_obs.is_event_day(res, "America/New_York", on_day) is True
        assert station_obs.is_event_day(res, "America/New_York", day_before) is False

    def test_local_event_date_handles_utc_midnight(self):
        # deadline stored as 00:00Z on Jul 9 = still Jul 8 in New York
        res = datetime(2026, 7, 9, 0, 0, tzinfo=timezone.utc)
        assert station_obs.local_event_date(res, "America/New_York") == date(2026, 7, 8)


class TestObservedExtremeClip:
    def _forecast(self, members, metric="temperature_2m_max"):
        return EnsembleForecast(
            lat=40.78, lon=-73.88, target_date=date(2026, 7, 8), metric=metric,
            member_arrays={"gfs_seamless": members},
            model_means={"gfs_seamless": float(np.mean(members)), "ecmwf_ifs025": float(np.mean(members))},
        )

    def _model(self):
        return ProbabilityModel(calibration_log_path=Path("/nonexistent.csv"))

    def test_max_members_clipped_up(self):
        # Ensemble centered at 30°C, but the station already recorded 35°C:
        # P(max ≥ 35) must go to ~certainty, P(bucket below) to ~zero.
        m = self._model()
        members = list(np.random.default_rng(1).normal(30.0, 1.0, 60))
        fc = self._forecast(members)
        p_above = m.compute_probability(fc, 34.5, "above", observed_extreme=35.0).raw_p
        p_low_range = m.compute_probability(fc, 29.0, "range", threshold_high=31.0,
                                            observed_extreme=35.0).raw_p
        assert p_above > 0.95
        assert p_low_range < 0.05

    def test_min_members_clipped_down(self):
        m = self._model()
        members = list(np.random.default_rng(2).normal(20.0, 1.0, 60))
        fc = self._forecast(members, metric="temperature_2m_min")
        # station already dropped to 15°C → P(min ≤ 15.5) ≈ 1
        p_below = m.compute_probability(fc, 15.5, "below", observed_extreme=15.0).raw_p
        assert p_below > 0.95

    def test_none_leaves_distribution_unchanged(self):
        m = self._model()
        members = list(np.random.default_rng(3).normal(30.0, 1.0, 60))
        fc = self._forecast(members)
        p0 = m.compute_probability(fc, 30.0, "above").raw_p
        p1 = m.compute_probability(fc, 30.0, "above", observed_extreme=None).raw_p
        assert p0 == pytest.approx(p1)

    def test_observation_below_threshold_barely_moves_p(self):
        # Observed 30.0 < threshold 30.5: clipping the lower tail to 30.0 adds
        # no mass above 30.5 — P must stay ~unchanged (exactness check).
        m = self._model()
        members = list(np.random.default_rng(4).normal(30.0, 1.0, 200))
        fc = self._forecast(members)
        p_plain = m.compute_probability(fc, 30.5, "above").raw_p
        p_clip = m.compute_probability(fc, 30.5, "above", observed_extreme=30.0).raw_p
        assert p_clip == pytest.approx(p_plain, abs=0.05)

    def test_observation_above_threshold_forces_certainty(self):
        # Observed 30.6 ≥ threshold 30.5: the event has already happened.
        m = self._model()
        members = list(np.random.default_rng(4).normal(30.0, 1.0, 200))
        fc = self._forecast(members)
        p = m.compute_probability(fc, 30.5, "above", observed_extreme=30.6).raw_p
        assert p > 0.95
