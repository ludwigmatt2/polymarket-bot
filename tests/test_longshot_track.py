"""Long-shot harvest track (Aug 2026): gate-rejected sub-3¢ YES signals are
logged to an isolated paper track for sample-gathering — never counted in
stats, the re-live gate, or calibrator training."""

import csv

import pytest

from tests.test_signal_generator import _make_generator, _make_market
from weather.paper_trader import PaperTrader


def _longshot_signal():
    # Model far above a 2¢ market → rejected (gate 9.7 low-priced YES and/or
    # gate 4.5 ceiling) but exactly the harvested shape.
    gen = _make_generator(model_p=0.30)
    sig = gen.evaluate(_make_market(yes_price=0.02))
    assert sig.quality_gate_passed is False
    assert sig.direction == "YES"
    return sig


def test_log_experimental_bypasses_gate(tmp_path):
    paper = PaperTrader(log_path=tmp_path / "paper_trades.csv")
    sig = _longshot_signal()
    assert paper.log_trade(sig) is None  # normal path stays shut
    t = paper.log_experimental(sig)
    assert t is not None
    assert t.scan_source == "longshot"
    assert t.size_usd == pytest.approx(5.0)
    row = list(csv.DictReader(open(tmp_path / "paper_trades.csv")))[0]
    assert row["scan_source"] == "longshot"


def test_log_experimental_dedupes(tmp_path):
    paper = PaperTrader(log_path=tmp_path / "paper_trades.csv")
    sig = _longshot_signal()
    assert paper.log_experimental(sig) is not None
    assert paper.log_experimental(sig) is None  # same (market, direction)


def test_longshot_rows_excluded_from_stats(tmp_path):
    paper = PaperTrader(log_path=tmp_path / "paper_trades.csv")
    t = paper.log_experimental(_longshot_signal())
    paper.resolve_trade(t.trade_id, actual_outcome=True)  # a jackpot win
    stats = paper.compute_stats()
    # The winning long-shot must not appear anywhere in the record.
    assert stats.total_trades == 0
    assert stats.resolved_trades == 0
    assert stats.station_resolved == 0
    assert stats.total_paper_pnl == pytest.approx(0.0)
