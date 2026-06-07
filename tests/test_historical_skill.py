"""Tests for Phase 1 historical-skill (MOS) pure logic — no network."""

import pytest

from build_historical_skill import (
    aggregate_cell,
    collect_errors,
    daily_from_hourly,
    validate_correction_levels,
    validate_mae_reduction,
)


def test_daily_from_hourly_max():
    times = ["2024-07-01T00:00", "2024-07-01T12:00", "2024-07-02T06:00"]
    vals = [20.0, 31.0, 25.0]
    out = daily_from_hourly(times, vals, max)
    assert out == {"2024-07-01": 31.0, "2024-07-02": 25.0}


def test_daily_from_hourly_skips_none():
    times = ["2024-07-01T00:00", "2024-07-01T12:00"]
    out = daily_from_hourly(times, [None, 28.0], max)
    assert out == {"2024-07-01": 28.0}


def test_collect_errors_buckets_by_lead_and_month():
    forecast = {1: {"2024-07-01": 32.0, "2024-08-01": 30.0}}
    actual = {"2024-07-01": 30.0, "2024-08-01": 31.0}
    errs = collect_errors(forecast, actual)
    assert errs[1][7] == [2.0]      # July: +2 warm
    assert errs[1][8] == [-1.0]     # August: -1 cold
    assert sorted(errs[1][0]) == [-1.0, 2.0]   # month 0 = all-months aggregate


def test_collect_errors_skips_missing_actual():
    forecast = {1: {"2024-07-01": 32.0, "2024-07-02": 33.0}}
    actual = {"2024-07-01": 30.0}
    errs = collect_errors(forecast, actual)
    assert errs[1][7] == [2.0]


def test_aggregate_cell():
    cell = aggregate_cell([1.0, 2.0, 3.0])
    assert cell["mean_error"] == pytest.approx(2.0)
    assert cell["n"] == 3
    assert aggregate_cell([]) is None


def test_validate_mae_reduction_rewards_real_bias():
    # consistent +3 warm bias → correcting by the mean should slash MAE
    errs = {1: [3.0, 3.1, 2.9, 3.0, 3.2, 2.8] * 5}
    rep = validate_mae_reduction(errs)
    assert rep[1]["reduction_pct"] > 50  # huge reduction for a real systematic bias


def test_validate_mae_reduction_no_gain_on_zero_mean_noise():
    # symmetric noise around 0 → no systematic bias to remove
    errs = {1: [2.0, -2.0] * 20}
    rep = validate_mae_reduction(errs)
    assert rep[1]["reduction_pct"] <= 1.0  # ~no improvement


def test_correction_levels_seasonal_beats_flat_when_bias_is_seasonal():
    # Two months with opposite, stable biases: month 6 = +4, month 12 = -4.
    # A flat mean ≈ 0 helps nothing; per-month correction removes almost all error.
    struct = {1: {
        6: [4.0, 4.1, 3.9, 4.0] * 10,
        12: [-4.0, -3.9, -4.1, -4.0] * 10,
    }}
    lv = validate_correction_levels([struct], min_cell=10)
    assert lv["seasonal_vs_flat_pct"] > 50      # seasonal slashes error
    assert lv["flat_mae"] > lv["seasonal_mae"]  # flat can't capture opposite-sign months


def test_correction_levels_flat_suffices_when_bias_is_constant():
    # Same +3 bias every month → flat already captures it; seasonal adds ~nothing.
    struct = {1: {m: [3.0, 3.1, 2.9, 3.0] * 10 for m in (6, 7, 8)}}
    lv = validate_correction_levels([struct], min_cell=10)
    assert abs(lv["seasonal_vs_flat_pct"]) < 5  # negligible difference
