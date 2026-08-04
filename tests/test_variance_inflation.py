"""Variance inflation (EMOS-lite, Aug 2026): members widen about their model's
mean BEFORE clip/pre-image/counting, so tail buckets regain probability mass.
λ=1 must be an exact no-op; the kill switch must revert to raw behavior."""

from datetime import date

import pytest

from weather import probability_model as pm
from weather.models import EnsembleForecast
from weather.probability_model import ProbabilityModel


def _forecast():
    # Two models tightly clustered around 30 °C — a tail bucket at 33+ gets
    # essentially zero raw mass until the spread is widened.
    return EnsembleForecast(10.0, 10.0, date(2026, 8, 1), "temperature_2m_max", {
        "gfs_seamless": [29.0, 29.5, 30.0, 30.5, 31.0] * 4,
        "ecmwf_ifs025": [29.2, 29.7, 30.2, 30.7, 31.2] * 4,
    })


def _model(tmp_path):
    return ProbabilityModel(calibration_log_path=tmp_path / "c.csv",
                            skill_corrector=None)


def test_lambda_one_is_noop(tmp_path, monkeypatch):
    m = _model(tmp_path)
    monkeypatch.setattr(pm, "VARIANCE_INFLATION", 1.0)
    base = m.compute_probability(_forecast(), 33.0, "above").raw_p
    monkeypatch.setattr(pm, "VARIANCE_INFLATION_ENABLED", False)
    off = m.compute_probability(_forecast(), 33.0, "above").raw_p
    assert base == pytest.approx(off)


def test_inflation_widens_tail_probability(tmp_path, monkeypatch):
    # 32.0 sits ~2.5σ above the ~30.1 ensemble mean (members 29-31.2): a real
    # tail bucket the raw ensemble prices at ~0.001 but λ=1.5 lifts to ~0.05 —
    # the exact underdispersion shape seen forward (raw<0.10 resolved 34.9%).
    m = _model(tmp_path)
    monkeypatch.setattr(pm, "VARIANCE_INFLATION", 1.0)
    base = m.compute_probability(_forecast(), 32.0, "above").raw_p
    monkeypatch.setattr(pm, "VARIANCE_INFLATION", 1.5)
    inflated = m.compute_probability(_forecast(), 32.0, "above").raw_p
    assert inflated > base  # tail bucket gains mass


def test_inflation_pulls_central_bucket_down(tmp_path, monkeypatch):
    # A bucket at the ensemble center loses mass when the distribution widens.
    m = _model(tmp_path)
    monkeypatch.setattr(pm, "VARIANCE_INFLATION", 1.0)
    base = m.compute_probability(_forecast(), 30.0, "equal").raw_p
    monkeypatch.setattr(pm, "VARIANCE_INFLATION", 1.5)
    inflated = m.compute_probability(_forecast(), 30.0, "equal").raw_p
    assert inflated < base


def test_kill_switch_reverts(tmp_path, monkeypatch):
    m = _model(tmp_path)
    monkeypatch.setattr(pm, "VARIANCE_INFLATION", 1.5)
    monkeypatch.setattr(pm, "VARIANCE_INFLATION_ENABLED", False)
    off = m.compute_probability(_forecast(), 33.0, "above").raw_p
    monkeypatch.setattr(pm, "VARIANCE_INFLATION", 1.0)
    monkeypatch.setattr(pm, "VARIANCE_INFLATION_ENABLED", True)
    base = m.compute_probability(_forecast(), 33.0, "above").raw_p
    assert off == pytest.approx(base)


def test_mean_preserved_under_inflation(tmp_path, monkeypatch):
    # Widening is about each model's own mean: P(above ensemble-mean) stays
    # ~0.5 regardless of λ — inflation adds dispersion, never bias.
    m = _model(tmp_path)
    monkeypatch.setattr(pm, "VARIANCE_INFLATION", 1.0)
    base = m.compute_probability(_forecast(), 30.1, "above").raw_p
    monkeypatch.setattr(pm, "VARIANCE_INFLATION", 1.6)
    inflated = m.compute_probability(_forecast(), 30.1, "above").raw_p
    assert abs(inflated - base) < 0.10
