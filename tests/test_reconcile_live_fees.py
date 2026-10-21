"""scripts/reconcile_live_fees.py — fee backfill + on-chain reconciliation."""
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "reconcile_live_fees", Path(__file__).resolve().parent.parent / "scripts/reconcile_live_fees.py")
rl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rl)


def _row(**kw):
    base = {"market_id": "0xabc", "resolved_at": "2026-10-01T00:00:00", "filled_size": "10",
            "filled_price": "0.40", "fee_usd": "", "market_title": "t"}
    base.update(kw)
    return base


def test_backfill_fills_only_blank_resolved_filled_rows():
    rows = [_row(), _row(fee_usd="0.5"), _row(resolved_at=""), _row(filled_size="0")]
    assert rl.backfill(rows) == 1
    assert rows[0]["fee_usd"] == round(10 * 0.40 * 0.05 * 0.60, 4)  # stake * 0.05 * (1-p)
    assert rows[1]["fee_usd"] == "0.5"
    assert rows[2]["fee_usd"] == "" and rows[3]["fee_usd"] == ""


def test_reconcile_flags_only_deviations_beyond_tolerance():
    rows = [_row()]
    rl.backfill(rows)
    exact = 10 * 0.40 + rows[0]["fee_usd"]
    assert rl.reconcile(rows, {"0xabc": exact}, 0.01) == []
    assert len(rl.reconcile(rows, {"0xabc": exact + 0.5}, 0.01)) == 1
