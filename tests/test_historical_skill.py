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


# ── Degenerate-write guard ────────────────────────────────────────────────────
# 2026-09-15: a transient Open-Meteo 429 storm failed all 15 cities and the run
# wrote {} over a good 30-city table, then exited 0 — the live model silently lost
# its MOS. These pin the guard that makes a lossy rebuild a FAILURE, not a smaller
# table. Driven through main() because the bug was in the write path, not the fit.

import json
import sys
from unittest.mock import patch

import build_historical_skill as bhs


def _run_main(tmp_path, monkeypatch, built, existing=None, argv=()):
    """Drive main() with the network stubbed to yield `built` targets."""
    skill = tmp_path / "historical_skill.json"
    if existing is not None:
        skill.write_text(json.dumps(existing))
    monkeypatch.setattr(bhs, "SKILL_PATH", skill)
    monkeypatch.setattr(bhs, "_load_cities",
                        lambda: [{"city": f"c{i}", "lat": float(i), "lon": float(i)}
                                 for i in range(len(built))])

    def fake_build(tgt):
        entry = built[int(tgt["city"][1:])]
        if entry is None:
            raise RuntimeError("429 Client Error: Too Many Requests")
        return entry, {}

    monkeypatch.setattr(bhs, "build_city", lambda t, s, e: fake_build(t))
    monkeypatch.setattr(sys, "argv", ["build_historical_skill.py", *argv])
    return skill


def _entry(i):
    return {"city": f"c{i}", "lat": float(i), "lon": float(i), "metrics": {}}


def test_every_target_failing_refuses_to_rewrite(tmp_path, monkeypatch):
    """The 429-storm shape: nothing built. Must exit non-zero so systemd reports a
    failure, and must leave the live table exactly as it was."""
    good = {"k1": {"city": "keep"}}
    skill = _run_main(tmp_path, monkeypatch, [None, None, None], existing=good)
    with pytest.raises(SystemExit) as e:
        bhs.main()
    assert "every target failed" in str(e.value)
    assert json.loads(skill.read_text()) == good


def test_partial_failure_keeps_the_entries_it_could_not_rebuild(tmp_path, monkeypatch):
    """1 of 3 cities built. Because the pass merges, the two it could not reach
    keep their existing entries rather than vanishing — a partial outage degrades
    freshness, never coverage."""
    existing = {f"{float(i)},{float(i)}": {"city": f"c{i}"} for i in range(3)}
    skill = _run_main(tmp_path, monkeypatch, [_entry(0), None, None], existing=existing)
    bhs.main()
    out = json.loads(skill.read_text())
    assert len(out) == 3
    assert out["1.0,1.0"]["city"] == "c1" and out["2.0,2.0"]["city"] == "c2"


def test_empty_table_is_still_refused(tmp_path, monkeypatch):
    """Fresh install, every target fails: nothing to merge into, nothing built."""
    skill = _run_main(tmp_path, monkeypatch, [None])
    with pytest.raises(SystemExit):
        bhs.main()
    assert not skill.exists()


def test_full_rebuild_writes_normally(tmp_path, monkeypatch):
    existing = {f"{float(i)},{float(i)}": {"city": f"c{i}"} for i in range(3)}
    skill = _run_main(tmp_path, monkeypatch, [_entry(0), _entry(1), _entry(2)],
                      existing=existing)
    bhs.main()
    assert len(json.loads(skill.read_text())) == 3


def test_city_rebuild_merges_and_keeps_station_entries(tmp_path, monkeypatch):
    """A city-only rebuild must REFRESH city entries without deleting the
    station-keyed ones. _nearest_city resolves every traded market to its
    resolving airport (NYC->KLGA, Miami->KMIA), so those entries are what the
    live model actually reads; starting from {} dropped all of them and left
    behind city entries nothing looks up. Sep 20 2026: the weekly timer hit
    exactly this and was only stopped by the shrink guard."""
    existing = {
        "40.78,-73.88": {"city": "KLGA", "lat": 40.78, "lon": -73.88, "metrics": {}},
        "25.79,-80.29": {"city": "KMIA", "lat": 25.79, "lon": -80.29, "metrics": {}},
        "0.0,0.0":      {"city": "c0", "lat": 0.0, "lon": 0.0, "metrics": {"stale": {}}},
    }
    skill = _run_main(tmp_path, monkeypatch, [_entry(0)], existing=existing)
    bhs.main()
    out = json.loads(skill.read_text())
    # station entries survive untouched
    assert out["40.78,-73.88"]["city"] == "KLGA"
    assert out["25.79,-80.29"]["city"] == "KMIA"
    # the rebuilt city entry is refreshed, not duplicated
    assert out["0.0,0.0"]["metrics"] == {}
    assert len(out) == 3
