"""Tests for Phase 1 HistoricalSkillCorrector and its compute_probability integration."""

import json
from datetime import date
from pathlib import Path

import pytest

from weather.probability_model import HistoricalSkillCorrector, ProbabilityModel
from weather.models import EnsembleForecast


def _write_skill(tmp_path, mean_error=2.0, n=50, month="6", lead="2") -> Path:
    table = {
        "10.0,10.0": {
            "city": "Test", "lat": 10.0, "lon": 10.0,
            "metrics": {
                "temperature_2m_max": {
                    lead: {month: {"mean_error": mean_error, "std_error": 1.0, "n": n},
                           "0": {"mean_error": 0.5, "std_error": 1.0, "n": 500}},
                },
            },
        }
    }
    p = tmp_path / "historical_skill.json"
    p.write_text(json.dumps(table))
    return p


def test_covers_only_enabled_metrics(tmp_path):
    c = HistoricalSkillCorrector(path=_write_skill(tmp_path))
    assert c.covers("temperature_2m_max")
    assert not c.covers("precipitation_sum")


def test_lookup_exact_cell(tmp_path):
    c = HistoricalSkillCorrector(path=_write_skill(tmp_path, mean_error=2.0))
    assert c.lookup_shift(10.0, 10.0, "temperature_2m_max", lead_day=2, month=6) == pytest.approx(2.0)


def test_lookup_falls_back_to_all_months_when_cell_thin(tmp_path):
    # month-6 cell has only n=5 (< MIN_SKILL_OBS) → fall back to the all-months (n=500) cell
    c = HistoricalSkillCorrector(path=_write_skill(tmp_path, mean_error=2.0, n=5))
    assert c.lookup_shift(10.0, 10.0, "temperature_2m_max", lead_day=2, month=6) == pytest.approx(0.5)


def test_lookup_none_for_far_location(tmp_path):
    c = HistoricalSkillCorrector(path=_write_skill(tmp_path))
    assert c.lookup_shift(50.0, 50.0, "temperature_2m_max", lead_day=2, month=6) is None


def test_lookup_none_for_disabled_metric(tmp_path):
    c = HistoricalSkillCorrector(path=_write_skill(tmp_path))
    assert c.lookup_shift(10.0, 10.0, "precipitation_sum", lead_day=2, month=6) is None


def test_adjust_members_shifts_down_for_warm_bias(tmp_path):
    c = HistoricalSkillCorrector(path=_write_skill(tmp_path, mean_error=2.0))
    out = c.adjust_members([25.0, 26.0], 10.0, 10.0, "temperature_2m_max", 2, 6)
    assert out == [23.0, 24.0]  # warm bias +2 → shifted down 2


def test_missing_file_is_noop():
    c = HistoricalSkillCorrector(path=Path("/nonexistent/historical_skill.json"))
    assert not c.is_loaded
    assert not c.covers("temperature_2m_max")
    assert c.adjust_members([25.0], 10.0, 10.0, "temperature_2m_max", 2, 6) == [25.0]


class TestComputeProbabilityIntegration:
    def _forecast(self):
        return EnsembleForecast(
            lat=10.0, lon=10.0, target_date=date(2024, 6, 15),
            metric="temperature_2m_max",
            member_arrays={"gfs_seamless": [26.0] * 20, "ecmwf_ifs025": [26.0] * 20},
        )

    def test_mos_shift_changes_raw_p(self, tmp_path):
        # warm bias +2 → members 26 shifted to 24 → P(above 25) drops from high to low
        model = ProbabilityModel(
            calibration_log_path=tmp_path / "cal.csv",
            skill_corrector=HistoricalSkillCorrector(path=_write_skill(tmp_path, mean_error=2.0)),
        )
        fc = self._forecast()
        without = model.compute_probability(fc, threshold=25.0, direction="above")
        with_mos = model.compute_probability(fc, threshold=25.0, direction="above", lead_day=2, month=6)
        assert without.raw_p > 0.8        # 26 > 25 → high
        assert with_mos.raw_p < without.raw_p  # shifted to 24 → lower

    def test_no_shift_without_lead_month(self, tmp_path):
        model = ProbabilityModel(
            calibration_log_path=tmp_path / "cal.csv",
            skill_corrector=HistoricalSkillCorrector(path=_write_skill(tmp_path, mean_error=2.0)),
        )
        fc = self._forecast()
        a = model.compute_probability(fc, threshold=25.0, direction="above")
        b = model.compute_probability(fc, threshold=25.0, direction="above", lead_day=2, month=6)
        assert a.raw_p != b.raw_p  # passing lead/month activates MOS
