"""Tests for scripts/lowedge_gate.py (pre-registered Gate 4 floor gate)."""

from __future__ import annotations

import csv
import importlib.util
from datetime import date, timedelta
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "lowedge_gate", Path(__file__).resolve().parent.parent / "scripts" / "lowedge_gate.py")
gate = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gate)

_D0 = date(2026, 10, 16)


def _day(i: int, span: int = 25) -> str:
    return (_D0 + timedelta(days=i % span)).isoformat()


def _row(edge, net, i=0, stake=10.0, span=25):
    return {"edge": edge, "stake": stake, "net": net, "t": f"{_day(i, span)}T08:00:00+00:00"}


def _band(edge, n, win_net, lose_net, win_frac, span=25):
    # win/lose pattern advances at a different stride than the day so day
    # clusters genuinely differ in their win/lose mix.
    return [_row(edge, win_net if ((i * 7) % 10) < win_frac * 10 else lose_net, i, span=span)
            for i in range(n)]


def test_dates_are_valid():
    for i in range(60):
        date.fromisoformat(_day(i))  # raises on an invalid calendar date


def test_below_n_or_weeks_is_never_supported():
    rows = _band(0.09, gate.MIN_N_BAND - 1, 5.0, -5.0, 0.9)
    assert gate.evaluate_band(rows, 0.08, 0.11, weeks=10)["supported"] is False
    rows = _band(0.09, gate.MIN_N_BAND, 5.0, -5.0, 0.9)
    r = gate.evaluate_band(rows, 0.08, 0.11, weeks=1)
    assert r["supported"] is False and "boot_p" not in r and r["decidable"] is False


def test_clearly_profitable_band_is_supported():
    rows = _band(0.09, gate.MIN_N_BAND, 6.0, -4.0, 0.8)
    r = gate.evaluate_band(rows, 0.08, 0.11, weeks=6)
    assert r["supported"] is True and r["roi"] > 0 and r["boot_p"] >= gate.BOOT_P_MIN


def test_losing_band_is_not_supported():
    rows = _band(0.09, gate.MIN_N_BAND, 6.0, -6.0, 0.3)
    assert gate.evaluate_band(rows, 0.08, 0.11, weeks=6)["supported"] is False


def test_profit_in_one_half_only_is_not_supported():
    # First half of the window is strongly positive, second half negative, with a
    # positive pooled ROI: the same-sign rule must reject it.
    early = [_row(0.09, 8.0, i, span=10) for i in range(100)]
    late = [{**_row(0.09, -2.0, i, span=10), "t": f"{(_D0 + timedelta(days=20 + i % 10)).isoformat()}T08:00:00+00:00"}
            for i in range(100)]
    r = gate.evaluate_band(early + late, 0.08, 0.11, weeks=6)
    assert r["roi"] > 0 and r["roi_second"] < 0 and r["supported"] is False


def test_halves_split_on_a_calendar_day_boundary():
    rows = [_row(0.09, 1.0, i, span=4) for i in range(40)]  # 4 days, 10 rows each
    first, second = gate._halves_by_day(rows)
    assert {r["t"][:10] for r in first}.isdisjoint({r["t"][:10] for r in second})
    assert gate._halves_by_day([_row(0.09, 1.0, 0, span=1)]) is None


def test_rows_outside_the_band_are_ignored():
    rows = _band(0.12, gate.MIN_N_BAND, 6.0, -4.0, 0.9)  # >= 0.11 duplicates production
    assert gate.evaluate_band(rows, 0.08, 0.11, weeks=6)["n"] == 0


def _res(decidable, supported):
    return {"decidable": decidable, "supported": supported}


def test_decision_table():
    assert gate.decide({"B1": _res(True, True), "B2": _res(True, True)}) == "LOWER_TO_0.02"
    assert gate.decide({"B1": _res(True, True), "B2": _res(True, False)}) == "LOWER_TO_0.05"
    assert gate.decide({"B1": _res(True, False), "B2": _res(True, True)}) == "KEEP"
    assert gate.decide({"B1": _res(True, False), "B2": _res(True, False)}) == "KEEP"


def test_b1_alone_decides_keep_even_when_b2_is_slow():
    # B1 failed with enough data; B2 still thin -> KEEP, no waiting for B2.
    assert gate.decide({"B1": _res(True, False), "B2": _res(False, False)}) == "KEEP"


def test_supported_b1_with_pending_b2_proposes_005_but_not_002():
    d = gate.decide({"B1": _res(True, True), "B2": _res(False, False)})
    assert d == "LOWER_TO_0.05_B2_PENDING"


def test_not_decidable_until_b1_has_data():
    assert gate.decide({"B1": _res(False, False), "B2": _res(True, True)}) is None


def _write(path, header, rows):
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


_COLS = ["actual_outcome", "label_source", "edge_pp", "entry_price", "size_usd", "pnl_usd",
         "signal_time", "fee_usd", "model_p", "direction"]


def test_loader_scores_at_the_ask_net_of_fee_and_filters(tmp_path):
    p = tmp_path / "paper_trades.csv"
    rows = [
        # NO row: p_win = 1-0.35 = 0.65; ask = 0.65 - 0.09 = 0.56 (mid entry 0.50). Win.
        ["1", "onchain", "0.09", "0.50", "20", "20.0", "2026-10-16T00:00:00+00:00", "", "0.35", "NO"],
        ["1", "grid", "0.09", "0.50", "20", "20.0", "2026-10-16T00:00:00+00:00", "", "0.35", "NO"],   # unverified truth
        ["", "onchain", "0.09", "0.50", "20", "", "2026-10-16T00:00:00+00:00", "", "0.35", "NO"],     # unresolved
        ["0", "onchain", "0.09", "0.03", "20", "-20", "2026-10-16T00:00:00+00:00", "", "", "NO"],     # below live floor
        # fallback to entry_price when model_p is missing; loss
        ["0", "station", "0.06", "0.40", "20", "-20", "2026-10-16T00:00:00+00:00", "0.6", "", "YES"],
    ]
    _write(p, _COLS, rows)
    out = gate.load_rows(p)
    assert len(out) == 2
    ask = 0.65 - 0.09
    expect = 20.0 * (1.0 / ask - 1.0) - 20.0 * 0.05 * (1.0 - ask)
    assert abs(out[0]["net"] - expect) < 1e-9
    assert out[0]["net"] < 20.0 - 20 * 0.05 * 0.5  # worse than the old mid-priced score
    assert abs(out[1]["net"] - (-20.0 - 20.0 * 0.05 * 0.6)) < 1e-9


def test_track_weeks_counts_from_first_logged_row_not_first_resolved(tmp_path):
    p = tmp_path / "paper_trades.csv"
    _write(p, _COLS, [
        ["", "", "0.09", "0.5", "20", "", "2026-10-16T00:00:00+00:00", "", "", "NO"],   # unresolved, oldest
        ["1", "onchain", "0.09", "0.5", "20", "5", "2026-10-30T00:00:00+00:00", "", "", "NO"],
    ])
    from datetime import datetime, timezone
    expected = (datetime.now(timezone.utc) - datetime(2026, 10, 16, tzinfo=timezone.utc)).total_seconds() / (7 * 86400)
    assert abs(gate.track_weeks(p) - expected) < 0.01


def test_track_weeks_flags_unparseable_timestamps(tmp_path, capsys):
    p = tmp_path / "paper_trades.csv"
    _write(p, _COLS, [["", "", "0.09", "0.5", "20", "", "2026-10-garbage", "", "", "NO"]])
    assert gate.track_weeks(p) == 0.0
    assert "unparseable" in capsys.readouterr().out
