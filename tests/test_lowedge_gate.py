"""Tests for scripts/lowedge_gate.py (pre-registered Gate 4 floor gate)."""

from __future__ import annotations

import csv
import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "lowedge_gate", Path(__file__).resolve().parent.parent / "scripts" / "lowedge_gate.py")
gate = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gate)


def _row(edge, net, i=0, stake=10.0):
    day = 16 + i % 25
    return {"edge": edge, "stake": stake, "net": net, "t": f"2026-10-{day:02d}T08:00:00+00:00"}


def _band(edge, n, win_net, lose_net, win_frac):
    return [_row(edge, win_net if (i % 10) < win_frac * 10 else lose_net, i) for i in range(n)]


def test_below_n_or_weeks_is_never_supported():
    rows = _band(0.09, gate.MIN_N_BAND - 1, 5.0, -5.0, 0.9)
    assert gate.evaluate_band(rows, 0.08, 0.11, weeks=10)["supported"] is False
    rows = _band(0.09, gate.MIN_N_BAND, 5.0, -5.0, 0.9)
    r = gate.evaluate_band(rows, 0.08, 0.11, weeks=1)
    assert r["supported"] is False and "boot_p" not in r


def test_clearly_profitable_band_is_supported():
    rows = _band(0.09, gate.MIN_N_BAND, 6.0, -4.0, 0.8)
    r = gate.evaluate_band(rows, 0.08, 0.11, weeks=6)
    assert r["supported"] is True and r["roi"] > 0 and r["boot_p"] >= gate.BOOT_P_MIN


def test_losing_band_is_not_supported():
    rows = _band(0.09, gate.MIN_N_BAND, 6.0, -6.0, 0.3)
    assert gate.evaluate_band(rows, 0.08, 0.11, weeks=6)["supported"] is False


def test_rows_outside_the_band_are_ignored():
    rows = _band(0.12, gate.MIN_N_BAND, 6.0, -4.0, 0.9)  # >= 0.11 duplicates production
    assert gate.evaluate_band(rows, 0.08, 0.11, weeks=6)["n"] == 0


def test_decision_table():
    ok, no = {"supported": True}, {"supported": False}
    assert gate.decide({"B1": ok, "B2": ok}) == "LOWER_TO_0.02"
    assert gate.decide({"B1": ok, "B2": no}) == "LOWER_TO_0.05"
    assert gate.decide({"B1": no, "B2": ok}) == "KEEP"  # a hole in the middle is noise
    assert gate.decide({"B1": no, "B2": no}) == "KEEP"


def test_loader_applies_fee_and_filters(tmp_path):
    p = tmp_path / "paper_trades.csv"
    cols = ["actual_outcome", "label_source", "edge_pp", "entry_price", "size_usd", "pnl_usd",
            "signal_time", "fee_usd"]
    rows = [
        ["1", "onchain", "0.09", "0.50", "20", "20.0", "2026-10-16T00:00:00+00:00", ""],   # kept, fee recomputed
        ["1", "grid", "0.09", "0.50", "20", "20.0", "2026-10-16T00:00:00+00:00", ""],      # unverified truth
        ["", "onchain", "0.09", "0.50", "20", "", "2026-10-16T00:00:00+00:00", ""],        # unresolved
        ["0", "onchain", "0.09", "0.03", "20", "-20", "2026-10-16T00:00:00+00:00", ""],    # below live floor
        ["0", "station", "0.06", "0.40", "20", "-20", "2026-10-16T00:00:00+00:00", "0.6"], # recorded fee
    ]
    with p.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerows(rows)
    out = gate.load_rows(p)
    assert len(out) == 2
    assert abs(out[0]["net"] - (20.0 - 20 * 0.05 * 0.5)) < 1e-9
    assert abs(out[1]["net"] - (-20.0 - 0.6)) < 1e-9
