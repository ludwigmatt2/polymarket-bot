"""Tests for Phase 2 confidence-scaled city bias correction."""

import csv
from pathlib import Path

import pytest

from weather.city_bias import CityBiasCorrector, FULL_CONFIDENCE_N, MIN_BIAS_N


def _write_bias(tmp_path, rows) -> Path:
    p = tmp_path / "city_bias.csv"
    with open(p, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["city", "lat", "lon", "n", "mean_bias_c", "damped_bias_c", "reliable"])
        w.writeheader()
        w.writerows(rows)
    return p


def test_loads_unreliable_cities_too(tmp_path):
    # Atlanta has reliable=0 in production but must now be loaded and applied.
    p = _write_bias(tmp_path, [
        {"city": "Atlanta", "lat": 33.75, "lon": -84.39, "n": 5,
         "mean_bias_c": 2.622, "damped_bias_c": 0.656, "reliable": 0},
    ])
    c = CityBiasCorrector(bias_path=p)
    # 2.622 * (5/15) ≈ 0.874 — NOT the double-damped 0.656 * 0.33
    assert c.get_offset(33.75, -84.39) == pytest.approx(2.622 * 5 / FULL_CONFIDENCE_N, abs=1e-3)


def test_full_confidence_at_threshold_n(tmp_path):
    p = _write_bias(tmp_path, [
        {"city": "X", "lat": 10.0, "lon": 10.0, "n": FULL_CONFIDENCE_N + 5,
         "mean_bias_c": 1.0, "damped_bias_c": 0.5, "reliable": 1},
    ])
    c = CityBiasCorrector(bias_path=p)
    assert c.get_offset(10.0, 10.0) == pytest.approx(1.0)  # confidence capped at 1.0


def test_below_min_n_is_dropped(tmp_path):
    p = _write_bias(tmp_path, [
        {"city": "Tiny", "lat": 20.0, "lon": 20.0, "n": MIN_BIAS_N - 1,
         "mean_bias_c": 3.0, "damped_bias_c": 0.3, "reliable": 0},
    ])
    c = CityBiasCorrector(bias_path=p)
    assert c.get_offset(20.0, 20.0) == 0.0


def test_distance_cutoff(tmp_path):
    p = _write_bias(tmp_path, [
        {"city": "X", "lat": 10.0, "lon": 10.0, "n": 20,
         "mean_bias_c": 1.0, "damped_bias_c": 0.5, "reliable": 1},
    ])
    c = CityBiasCorrector(bias_path=p)
    assert c.get_offset(50.0, 50.0) == 0.0  # >100 km away


def test_negative_bias_preserved(tmp_path):
    p = _write_bias(tmp_path, [
        {"city": "Paris", "lat": 48.85, "lon": 2.35, "n": 5,
         "mean_bias_c": -1.88, "damped_bias_c": -0.47, "reliable": 0},
    ])
    c = CityBiasCorrector(bias_path=p)
    assert c.get_offset(48.85, 2.35) == pytest.approx(-1.88 * 5 / FULL_CONFIDENCE_N, abs=1e-3)
