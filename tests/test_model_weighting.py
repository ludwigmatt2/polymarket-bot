"""Tests for Phase 4 per-model skill weighting + model_skill_tracker."""

from datetime import date
from pathlib import Path

import pytest

from weather.probability_model import ProbabilityModel, _fraction_satisfying, _apply_kde
from weather.models import EnsembleForecast
from model_skill_tracker import parse_breakdown, score_models, suggest_weights


# ── Weighted helpers ─────────────────────────────────────────────────────────────

def test_uniform_weights_equal_unweighted():
    mem = [float(i) for i in range(20)]
    a = _fraction_satisfying(mem, 10.0, "above")
    b = _fraction_satisfying(mem, 10.0, "above", weights=[1.0] * 20)
    assert a == pytest.approx(b)


def test_weighted_fraction_amplifies_weighted_members():
    # 5 members below (weight 1), 5 above (weight 3) → weighted P(above) = 15/20 = 0.75
    members = [0.0] * 5 + [10.0] * 5
    weights = [1.0] * 5 + [3.0] * 5
    assert _fraction_satisfying(members, 5.0, "above", weights=weights) == pytest.approx(0.75)


def test_kde_accepts_weights_without_error():
    members = [float(i) for i in range(30)]
    weights = [1.0] * 15 + [2.0] * 15
    p = _apply_kde(members, 15.0, "above", None, 0.5, weights=weights)
    assert 0.0 <= p <= 1.0


# ── compute_probability integration ──────────────────────────────────────────────

def _forecast():
    # ECMWF (50) above 25; GFS(31)+ICON(40) below
    return EnsembleForecast(10.0, 10.0, date(2024, 6, 15), "temperature_2m_max", {
        "gfs_seamless": [20.0] * 31, "icon_seamless": [20.0] * 40, "ecmwf_ifs025": [30.0] * 50})


def test_equal_weights_match_member_pooling(tmp_path):
    m = ProbabilityModel(calibration_log_path=tmp_path / "c.csv",
                         model_weights={"gfs_seamless": 1, "icon_seamless": 1, "ecmwf_ifs025": 1})
    raw = m.compute_probability(_forecast(), 25.0, "above").raw_p
    assert raw == pytest.approx(50 / 121, abs=0.01)  # ECMWF member share


def test_prior_amplifies_ecmwf(tmp_path):
    m_eq = ProbabilityModel(calibration_log_path=tmp_path / "a.csv",
                            model_weights={"gfs_seamless": 1, "icon_seamless": 1, "ecmwf_ifs025": 1})
    m_pr = ProbabilityModel(calibration_log_path=tmp_path / "b.csv")  # literature prior
    eq = m_eq.compute_probability(_forecast(), 25.0, "above").raw_p
    pr = m_pr.compute_probability(_forecast(), 25.0, "above").raw_p
    assert pr > eq  # up-weighting the (above) ECMWF raises P(above)
    assert pr == pytest.approx(0.5, abs=0.02)  # 50*1.5/150


# ── model_skill_tracker ──────────────────────────────────────────────────────────

def test_parse_breakdown():
    assert parse_breakdown('{"gfs_seamless": 0.4, "ecmwf_ifs025": 0.6}') == {
        "gfs_seamless": 0.4, "ecmwf_ifs025": 0.6}
    assert parse_breakdown("") == {}
    assert parse_breakdown("not json") == {}


def test_score_models_brier():
    rows = [
        ({"gfs_seamless": 0.0, "ecmwf_ifs025": 1.0}, 1),  # gfs wrong, ecmwf right
        ({"gfs_seamless": 0.0, "ecmwf_ifs025": 1.0}, 1),
    ]
    scores = score_models(rows)
    assert scores["ecmwf_ifs025"]["brier"] == pytest.approx(0.0)
    assert scores["gfs_seamless"]["brier"] == pytest.approx(1.0)
    assert scores["ecmwf_ifs025"]["n"] == 2


def test_suggest_weights_none_when_undersampled():
    rows = [({"gfs_seamless": 0.5, "ecmwf_ifs025": 0.5}, 1)] * 5  # n=5 < MIN_MODEL_OBS
    assert suggest_weights(score_models(rows)) is None


def test_suggest_weights_orders_by_skill():
    # ecmwf brier 0.04, gfs brier 0.25 → ecmwf weight = 0.25/0.04 = 6.25, gfs = 1.0
    rows = [({"gfs_seamless": 0.5, "ecmwf_ifs025": 0.8}, 1)] * 40
    w = suggest_weights(score_models(rows), min_obs=30)
    assert w is not None
    assert w["gfs_seamless"] == pytest.approx(1.0)
    assert w["ecmwf_ifs025"] > w["gfs_seamless"]
