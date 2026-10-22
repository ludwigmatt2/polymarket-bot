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


def _wallet(monkeypatch, deposited, withdrawn, pusd, deployed, pnl):
    import weather.live_ledger as ll
    import weather.relayer as rl
    import weather.secrets as sec
    monkeypatch.setattr(ll, "totals", lambda p: {"deposited": deposited, "withdrawn": withdrawn,
                                                 "net": deposited - withdrawn})
    monkeypatch.setattr(sec, "get_user_creds", lambda uid: {"funder_address": "0xabc"})
    monkeypatch.setattr(rl, "pusd_balance", lambda a, strict=False: pusd)
    monkeypatch.setattr(rl, "usdce_balance", lambda a, strict=False: 0.0)
    ls = {"total_pnl": pnl, "all_time_pnl": pnl, "deployed": deployed, "pending": 3}
    return tb.live_wallet_stats(1, ls)


def test_return_on_deposits_is_wallet_based(monkeypatch):
    ws = _wallet(monkeypatch, deposited=100.0, withdrawn=0.0, pusd=108.0, deployed=13.0, pnl=39.58)
    assert abs(ws["wallet_gain"] - 21.0) < 1e-9          # 108 + 13 - 100
    assert abs(ws["total_return_pct"] - 21.0) < 1e-9
    assert ws["return_pct"] == ws["total_return_pct"]     # one definition everywhere


def test_withdrawals_count_as_returned_capital(monkeypatch):
    ws = _wallet(monkeypatch, deposited=100.0, withdrawn=30.0, pusd=90.0, deployed=0.0, pnl=20.0)
    assert abs(ws["wallet_gain"] - 20.0) < 1e-9          # 90 + 30 - 100
