"""Tests for scripts/kelly_scale_gate.py and scripts/lead_time_gate.py."""

from __future__ import annotations

import csv
import importlib.util
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


kelly = _load("kelly_scale_gate")
lead = _load("lead_time_gate")


def _fill(i, win, stake=4.0, px=0.5):
    # win returns +stake*(1/px-1) = +stake at px=0.5; loss returns -stake.
    pnl = stake if win else -stake
    day = 16 + i % 20
    return {"stake": stake, "px": px, "pnl": pnl, "gross_win": win, "edge": 0.13,
            "t": f"2026-10-{day:02d}T08:00:00+00:00", "res": f"2026-10-{day:02d}T20:{i % 60:02d}:00+00:00"}


# ── kelly_scale_gate ────────────────────────────────────────────────────────

def test_no_edge_series_has_zero_growth_optimal_multiplier():
    # 40% wins at even odds = negative edge; hindsight-optimal stake is zero.
    rows = [_fill(i, win=i % 5 < 2) for i in range(60)]
    assert kelly.best_multiplier(rows) == 0.0


def test_strong_edge_series_pins_to_top_of_grid():
    rows = [_fill(i, win=i % 10 != 0) for i in range(60)]  # 90% wins at even odds
    assert kelly.best_multiplier(rows) == max(kelly.GRID)


def test_not_answerable_below_min_n_and_no_bootstrap_runs():
    r = kelly.evaluate([_fill(i, win=i % 2 == 0) for i in range(kelly.MIN_N - 1)])
    assert r["answerable"] is False and "p_reduce" not in r


def test_proceed_reduce_when_no_realized_edge():
    rows = [_fill(i, win=i % 5 < 2) for i in range(kelly.MIN_N)]
    r = kelly.evaluate(rows)
    assert r["answerable"] and r["proceed"] is True and r["s_median"] == 0.0


def test_keep_sizing_when_edge_is_large():
    rows = [_fill(i, win=i % 10 != 0) for i in range(kelly.MIN_N)]
    r = kelly.evaluate(rows)
    assert r["answerable"] and r["proceed"] is False


def test_kelly_loader_filters_unresolved_and_unfilled(tmp_path):
    p = tmp_path / "live_trades.csv"
    cols = ["order_status", "filled_size", "filled_price", "actual_outcome", "pnl_usd",
            "edge_pp", "signal_time", "resolved_at"]
    rows = [["matched", "10", "0.5", "1", "5.0", "0.13", "2026-10-16T00:00:00+00:00", "2026-10-17T00:00:00+00:00"],
            ["matched", "10", "0.5", "", "", "0.13", "2026-10-16T00:00:00+00:00", ""],
            ["failed", "0", "", "", "", "", "2026-10-16T00:00:00+00:00", ""]]
    with p.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerows(rows)
    out = kelly.load_fills(p)
    assert len(out) == 1 and out[0]["stake"] == 5.0
    # pnl is NET of the taker fee: 5.0 - 5.0*0.05*(1-0.5)
    assert abs(out[0]["pnl"] - 4.875) < 1e-9 and out[0]["gross_win"] is True


def test_lead_loaders_subtract_fee(tmp_path):
    cols = ["order_status", "filled_size", "filled_price", "actual_outcome", "pnl_usd",
            "signal_time", "resolution_date"]
    p = tmp_path / "live_trades.csv"
    with p.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerow(["matched", "10", "0.5", "1", "5.0", "2026-10-16T00:00:00+00:00", "2026-10-16T06:00:00+00:00"])
    out = lead.load_live(p)
    assert len(out) == 1 and abs(out[0]["pnl"] - 4.875) < 1e-9 and out[0]["lead"] == 6.0
    pc = tmp_path / "paper_trades.csv"
    with pc.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["entry_price", "size_usd", "actual_outcome", "pnl_usd", "metric",
                    "signal_time", "resolution_date"])
        w.writerow(["0.4", "20", "1", "30", "temperature_2m_max",
                    "2026-10-16T00:00:00+00:00", "2026-10-16T06:00:00+00:00"])
    pout = lead.load_paper(pc)
    assert abs(pout[0]["pnl"] - (30 - 20 * 0.05 * 0.6)) < 1e-9


# ── lead_time_gate ──────────────────────────────────────────────────────────

def _lrow(i, short, win, stake=4.0):
    day = 16 + i % 20
    return {"stake": stake, "pnl": stake if win else -stake,
            "lead": 6.0 if short else 30.0, "t": f"2026-10-{day:02d}T08:00:00+00:00"}


def _set(n, short_win_rate, long_win_rate):
    rows = []
    for i in range(n):
        short = i % 4 == 0
        rate = short_win_rate if short else long_win_rate
        rows.append(_lrow(i, short, win=(i * 7 % 100) < rate * 100))
    return rows


def test_lead_hours_and_cut():
    h = lead._lead_hours("2026-10-15T02:00:00+00:00", "2026-10-15T14:00:00+00:00")
    assert h == 12.0 and not (h < lead.CUT_HOURS)


def test_lead_not_answerable_with_few_short_rows():
    rows = [_lrow(i, short=False, win=True) for i in range(lead.MIN_N)]
    assert lead.evaluate_set(rows)["answerable"] is False


def test_lead_proceed_requires_both_sets():
    good = lead.evaluate_set(_set(lead.MIN_N, 0.8, 0.4))
    bad = lead.evaluate_set(_set(lead.MIN_N, 0.4, 0.8))
    assert good["answerable"] and good["diff"] > 0
    assert lead.verdict(good, good)["proceed"] is True
    assert lead.verdict(good, bad)["proceed"] is False  # paper contradicts live
    assert lead.verdict(bad, bad)["proceed"] is False


def test_lead_paper_rows_are_scored_at_the_ask_not_the_mid(tmp_path):
    pc = tmp_path / "paper_trades.csv"
    with pc.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["entry_price", "size_usd", "actual_outcome", "pnl_usd", "metric", "signal_time",
                    "resolution_date", "model_p", "edge_pp", "direction"])
        # NO: p_win 0.65, ask 0.56, mid entry 0.50, win.
        w.writerow(["0.50", "20", "1", "20.0", "temperature_2m_max",
                    "2026-10-16T00:00:00+00:00", "2026-10-16T06:00:00+00:00", "0.35", "0.09", "NO"])
    out = lead.load_paper(pc)
    ask = 0.56
    assert abs(out[0]["pnl"] - (20.0 * (1 / ask - 1) - 20.0 * 0.05 * (1 - ask))) < 1e-9
