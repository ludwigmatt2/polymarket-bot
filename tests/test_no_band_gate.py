"""Tests for scripts/no_band_gate.py (pre-registered NO entry-price gate)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "no_band_gate",
    Path(__file__).resolve().parent.parent / "scripts" / "no_band_gate.py",
)
gate = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gate)


def _row(direction, px, pnl, day=1):
    return {"dir": direction, "px": px, "pnl": pnl, "t": f"2026-11-{day:02d}T12:00:00+00:00"}


def _cheap(n):
    return [_row("YES", 0.25, 1.0, day=1 + i % 20) for i in range(n)]


def test_only_no_at_or_above_cut_is_dropped():
    rows = [_row("NO", 0.45, -5.0), _row("NO", 0.30, 4.0), _row("YES", 0.60, -3.0)]
    r = gate.evaluate(rows)
    assert r["n_drop"] == 1
    assert abs(r["drop_pnl"] - (-5.0)) < 1e-9
    assert abs(r["keep_pnl"] - 1.0) < 1e-9  # NO@0.30 (+4) and YES (-3) are kept


def test_not_answerable_below_thresholds():
    r = gate.evaluate(_cheap(20) + [_row("NO", 0.5, -1.0)] * 5)
    assert r["answerable"] is False
    assert "boot_p" not in r  # no verdict machinery runs early


def test_requires_drop_n_even_when_total_n_is_enough():
    r = gate.evaluate(_cheap(gate.MIN_N) + [_row("NO", 0.5, -1.0)] * 3)
    assert r["n"] >= gate.MIN_N and r["answerable"] is False


def test_proceed_when_dropped_set_loses_consistently():
    drop = [_row("NO", 0.5, -1.0, day=1 + i % 20) for i in range(gate.MIN_DROP_N)]
    r = gate.evaluate(_cheap(gate.MIN_N) + drop)
    assert r["answerable"] and r["both_neg"] and r["proceed"] is True


def test_no_proceed_when_dropped_set_wins():
    drop = [_row("NO", 0.5, 1.0, day=1 + i % 20) for i in range(gate.MIN_DROP_N)]
    r = gate.evaluate(_cheap(gate.MIN_N) + drop)
    assert r["answerable"] and r["proceed"] is False


def test_no_proceed_when_loss_is_only_in_one_half():
    half = gate.MIN_DROP_N // 2
    drop = ([_row("NO", 0.5, -3.0, day=1 + i % 10) for i in range(half)]
            + [_row("NO", 0.5, 1.0, day=15 + i % 10) for i in range(gate.MIN_DROP_N - half)])
    r = gate.evaluate(_cheap(gate.MIN_N) + drop)
    assert r["answerable"] and r["both_neg"] is False and r["proceed"] is False
