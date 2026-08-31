"""Per-cell EMOS dispersion: λ scales with each cell's historical forecast-error
std, anchored so the mean trusted cell reproduces the global λ, clamped to a band.
Reuses the skill table MOS already loads (cells carry std_error)."""

from datetime import date

import pytest

from weather.models import EnsembleForecast
from weather.probability_model import (
    DispersionCorrector,
    HistoricalSkillCorrector,
    ProbabilityModel,
)


def _skill_with_cells():
    """A HistoricalSkillCorrector with an injected table: one hard cell (Aug,
    std 3.0) and one calm cell (Jul, std 1.0). Mean trusted std = 2.0."""
    sk = HistoricalSkillCorrector.__new__(HistoricalSkillCorrector)
    sk.enabled_metrics = frozenset({"temperature_2m_max"})
    sk.max_distance_km = 100.0
    sk.load_error = None
    sk._cities = [{
        "city": "Test", "lat": 25.77, "lon": -80.19,
        "metrics": {"temperature_2m_max": {"1": {
            "8": {"mean_error": 0.0, "std_error": 3.0, "n": 200},   # hard month
            "7": {"mean_error": 0.0, "std_error": 1.0, "n": 200},   # calm month
        }}},
    }]
    return sk


def test_harder_cell_gets_more_inflation():
    d = DispersionCorrector(skill=_skill_with_cells(), base_lambda=2.0)
    assert d.ref_std == pytest.approx(2.0)
    hard = d.lookup_lambda(25.77, -80.19, "temperature_2m_max", 1, 8)
    calm = d.lookup_lambda(25.77, -80.19, "temperature_2m_max", 1, 7)
    assert hard == pytest.approx(2.0 * 3.0 / 2.0)   # 3.0
    assert calm == pytest.approx(1.25)               # 2.0*1.0/2.0=1.0 → clamped to lam_min
    assert hard > calm


def test_lambda_clamped_to_band():
    d = DispersionCorrector(skill=_skill_with_cells(), base_lambda=2.0, lam_min=1.5, lam_max=2.5)
    hard = d.lookup_lambda(25.77, -80.19, "temperature_2m_max", 1, 8)  # raw 3.0
    assert hard == pytest.approx(2.5)  # clamped down to lam_max


def test_none_when_no_trusted_cell():
    d = DispersionCorrector(skill=_skill_with_cells(), base_lambda=2.0)
    # far-away location → no nearest city within max_distance_km
    assert d.lookup_lambda(0.0, 0.0, "temperature_2m_max", 1, 8) is None


def test_none_when_table_empty():
    empty = HistoricalSkillCorrector.__new__(HistoricalSkillCorrector)
    empty.enabled_metrics = frozenset()
    empty.max_distance_km = 100.0
    empty.load_error = None
    empty._cities = []
    d = DispersionCorrector(skill=empty, base_lambda=2.0)
    assert not d.is_loaded
    assert d.lookup_lambda(25.77, -80.19, "temperature_2m_max", 1, 8) is None


def test_model_uses_percell_lambda_over_scalar(tmp_path):
    """A model with the dispersion corrector inflates the hard cell more than a
    flat-scalar model, so a tail bucket gains more mass."""
    fc = EnsembleForecast(25.77, -80.19, date(2026, 8, 5), "temperature_2m_max", {
        "gfs_seamless": [86.0, 88.0, 90.0, 92.0, 94.0] * 4,
    })
    scalar = ProbabilityModel(calibration_log_path=tmp_path / "emos_a.csv", skill_corrector=None,
                              variance_inflation=2.0)
    percell = ProbabilityModel(calibration_log_path=tmp_path / "emos_b.csv", skill_corrector=None,
                               variance_inflation=2.0,
                               dispersion_corrector=DispersionCorrector(skill=_skill_with_cells(),
                                                                        base_lambda=2.0))
    # Aug (month 8) hard cell → λ 3.0 > scalar 2.0 → fatter tail at 96.
    tail_scalar = scalar.compute_probability(fc, 96.0, "above", lead_day=1, month=8).raw_p
    tail_percell = percell.compute_probability(fc, 96.0, "above", lead_day=1, month=8).raw_p
    assert tail_percell > tail_scalar
