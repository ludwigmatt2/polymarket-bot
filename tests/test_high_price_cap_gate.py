"""Tests for scripts/high_price_cap_gate.py (pre-registered sizing-cap gate)."""

from __future__ import annotations

import csv
import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "high_price_cap_gate",
    Path(__file__).resolve().parent.parent / "scripts" / "high_price_cap_gate.py",
)
gate = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gate)


def _row(px, stake, pnl, day=1):
    return {"px": px, "stake": stake, "pnl": pnl, "t": f"2026-10-{day:02d}T12:00:00+00:00"}


def _mid(n, stake=3.0):
    return [_row(0.5, stake, 1.0, day=16 + i % 10) for i in range(n)]


def test_cap_only_touches_band_and_scales_pnl_linearly():
    rows = _mid(10) + [_row(0.75, 9.0, -9.0), _row(0.65, 2.0, 1.0)]
    r = gate.evaluate(rows)
    assert r["ref"] == 3.0 and r["cap"] == 3.0
    # 9.0 stake capped to 3.0 -> loss shrinks from -9 to -3 (delta +6); the
    # 2.0 stake is under the cap and unchanged.
    assert abs(r["delta"] - 6.0) < 1e-9
    assert abs(r["base"] - (-8.0)) < 1e-9


def test_not_answerable_below_thresholds():
    r = gate.evaluate(_mid(20) + [_row(0.7, 6.0, -6.0)] * 5)
    assert r["answerable"] is False
    assert "boot_p" not in r  # no verdict machinery runs early


def test_requires_band_n_even_when_total_n_is_enough():
    r = gate.evaluate(_mid(gate.MIN_N) + [_row(0.7, 6.0, -6.0)] * 3)
    assert r["n"] >= gate.MIN_N and r["answerable"] is False


def test_proceed_when_cap_helps_consistently():
    # Big stakes lose, small ones win, spread over many days and both halves.
    band = []
    for i in range(gate.MIN_BAND_N):
        day = 16 + i % 12
        band.append(_row(0.7, 9.0, -9.0, day) if i % 2 else _row(0.7, 2.0, 1.0, day))
    r = gate.evaluate(_mid(gate.MIN_N) + band)
    assert r["answerable"] and r["delta"] > 0 and r["same_sign"]
    assert r["proceed"] is True


def test_no_proceed_when_cap_gives_up_winners():
    band = [_row(0.7, 9.0, 3.0, 16 + i % 12) for i in range(gate.MIN_BAND_N)]
    r = gate.evaluate(_mid(gate.MIN_N) + band)
    assert r["answerable"] and r["delta"] < 0 and r["proceed"] is False


def test_load_fills_filters_unresolved_and_unfilled(tmp_path):
    p = tmp_path / "live_trades.csv"
    cols = ["order_status", "filled_size", "filled_price", "actual_outcome", "pnl_usd", "signal_time"]
    rows = [
        ["matched", "10", "0.5", "1", "5.0", "2026-10-16T00:00:00+00:00"],   # kept
        ["matched", "10", "0.5", "", "", "2026-10-16T00:00:00+00:00"],       # open
        ["failed", "0", "", "", "", "2026-10-16T00:00:00+00:00"],            # not filled
    ]
    with p.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerows(rows)
    out = gate.load_fills(p)
    # pnl is NET of the taker fee: 5.0 - 5.0 * 0.05 * (1 - 0.5)
    assert len(out) == 1 and out[0]["stake"] == 5.0 and abs(out[0]["pnl"] - 4.875) < 1e-9
