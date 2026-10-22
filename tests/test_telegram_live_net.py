"""telegram_bot live stats are NET of the Polymarket taker fee (pnl_usd is gross)."""
import csv

import telegram_bot as tb

FIELDS = ["signal_time", "resolved_at", "size_usd", "filled_size", "filled_price", "pnl_usd", "fee_usd", "error"]


def _write(path, rows):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})


def _stats(tmp_path, monkeypatch, rows):
    p = tmp_path / "live_trades.csv"
    _write(p, rows)
    monkeypatch.setattr(tb, "_live_trades_csv_path", lambda uid: p)
    monkeypatch.setattr(tb, "_live_era_start", lambda uid: "2026-08-20")
    return tb.read_live_stats(1)


def test_pnl_roi_and_pf_are_net_of_fee(tmp_path, monkeypatch):
    rows = [
        # win: +6 gross, fee recorded 0.12
        dict(signal_time="2026-09-01", resolved_at="2026-09-02T00:00:00", size_usd=4, filled_size=10,
             filled_price=0.4, pnl_usd=6.0, fee_usd=0.12),
        # loss: -4 gross, fee blank -> recomputed 0.05*0.4*0.6*10 = 0.12
        dict(signal_time="2026-09-01", resolved_at="2026-09-02T00:00:00", size_usd=4, filled_size=10,
             filled_price=0.4, pnl_usd=-4.0, fee_usd=""),
    ]
    ls = _stats(tmp_path, monkeypatch, rows)
    assert abs(ls["fees"] - 0.24) < 1e-9
    assert abs(ls["total_pnl"] - (2.0 - 0.24)) < 1e-9
    assert abs(ls["era_roi"] - 100 * (2.0 - 0.24) / (8 + 0.24)) < 1e-9
    assert abs(ls["profit_factor"] - 5.88 / 4.12) < 1e-9


def test_tiny_gross_win_that_fee_turns_into_loss_counts_as_loss(tmp_path, monkeypatch):
    rows = [dict(signal_time="2026-09-01", resolved_at="2026-09-02T00:00:00", size_usd=4, filled_size=10,
                 filled_price=0.4, pnl_usd=0.05, fee_usd=0.12)]
    ls = _stats(tmp_path, monkeypatch, rows)
    assert ls["wins"] == 0 and ls["losses"] == 1


def test_all_time_pnl_for_ledger_balance_is_net(tmp_path, monkeypatch):
    rows = [dict(signal_time="2026-07-03", resolved_at="2026-07-04T00:00:00", size_usd=4, filled_size=10,
                 filled_price=0.4, pnl_usd=6.0, fee_usd=0.12)]  # before era start
    ls = _stats(tmp_path, monkeypatch, rows)
    assert ls["resolved"] == 0 and abs(ls["all_time_pnl"] - 5.88) < 1e-9
