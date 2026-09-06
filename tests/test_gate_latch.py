"""Tests for the go-live gate latch (LIVE_GATE_LATCHES).

The gate qualifies ENTRY into live trading; once it has ever passed it latches
open, so a later dip in paper stats does not re-halt an already-proven bot. The
per-day loss kill switch remains the live-side backstop (tested elsewhere).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from weather.live_trader import LiveTrader


def _trader(tmp_path: Path, ready: bool) -> LiveTrader:
    paper = MagicMock()
    paper.compute_stats.return_value = MagicMock(ready_for_live=ready)
    return LiveTrader(
        paper_trader=paper,
        bankroll_usd=100.0,
        fill_poll_delay=0.0,
        log_path=tmp_path / "live_trades.csv",
        idempotency_path=tmp_path / "live_idempotency.json",
    )


def test_first_pass_unlocks_and_writes_latch(tmp_path):
    t = _trader(tmp_path, ready=True)
    assert not t._latch_path.exists()
    assert t.is_unlocked() is True
    assert t._latch_path.exists()  # latched on first pass


def test_latch_keeps_live_unlocked_after_stats_dip(tmp_path):
    """The core new behaviour: once passed, a later sub-threshold stat keeps live."""
    t = _trader(tmp_path, ready=True)
    assert t.is_unlocked() is True          # passes, latches
    # Stats now degrade below the bar (e.g. PF 1.43 < 1.5).
    t.paper_trader.compute_stats.return_value = MagicMock(ready_for_live=False)
    assert t.is_unlocked() is True          # STILL unlocked — entry gate, not a leash
    # A fresh trader in the same dir (simulating a restart) also stays unlocked.
    assert _trader(tmp_path, ready=False).is_unlocked() is True


def test_never_passed_stays_locked(tmp_path):
    t = _trader(tmp_path, ready=False)
    assert t.is_unlocked() is False
    assert not t._latch_path.exists()       # nothing latched — never qualified


def test_flag_off_rechecks_every_call(tmp_path, monkeypatch):
    import weather.live_trader as lt_mod
    monkeypatch.setattr(lt_mod, "LIVE_GATE_LATCHES", False)
    t = _trader(tmp_path, ready=True)
    assert t.is_unlocked() is True
    assert not t._latch_path.exists()       # no latch written when flag off
    t.paper_trader.compute_stats.return_value = MagicMock(ready_for_live=False)
    assert t.is_unlocked() is False         # original behaviour: re-checks, re-locks
