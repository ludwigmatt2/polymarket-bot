"""PR3 — mode-scoped stats: live accounts report real money only; paper preserved."""

import csv as _csv
import os

os.environ.setdefault("POLYMARKET_BOT_TOKEN", "test:token")
os.environ.setdefault("TELEGRAM_ADMIN_ID", "1")

import telegram_bot as tb
from weather import live_ledger as L

_LIVE_COLS = ["trade_id", "resolved_at", "pnl_usd", "size_usd", "error", "resolution_date"]


def _write_live_csv(path, rows):
    with open(path, "w", newline="") as f:
        w = _csv.DictWriter(f, fieldnames=_LIVE_COLS)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in _LIVE_COLS})


class TestReadLiveStats:
    def test_counts_pnl_deployed_and_excludes_errors(self, tmp_path, monkeypatch):
        csv_path = tmp_path / "live_trades.csv"
        _write_live_csv(csv_path, [
            {"trade_id": "a", "resolved_at": "2026-07-01T00:00:00", "pnl_usd": "5", "size_usd": "10"},
            {"trade_id": "b", "resolved_at": "2026-07-01T00:00:00", "pnl_usd": "-3", "size_usd": "10"},
            {"trade_id": "c", "size_usd": "8", "resolution_date": "2026-07-05"},          # open
            {"trade_id": "d", "size_usd": "5", "error": "insufficient balance"},          # excluded
        ])
        monkeypatch.setattr(tb, "_live_trades_csv_path", lambda uid: csv_path)
        s = tb.read_live_stats(1)
        assert s["total"] == 3 and s["resolved"] == 2 and s["pending"] == 1
        assert s["wins"] == 1 and s["losses"] == 1 and s["win_rate"] == 50.0
        assert round(s["profit_factor"], 3) == round(5 / 3, 3)
        assert s["total_pnl"] == 2.0 and s["deployed"] == 8.0

    def test_missing_file_is_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tb, "_live_trades_csv_path", lambda uid: tmp_path / "nope.csv")
        assert tb.read_live_stats(1) == {}


class TestLiveWalletStats:
    def test_return_from_real_deposits(self, tmp_path, monkeypatch):
        csv_path = tmp_path / "live_trades.csv"
        _write_live_csv(csv_path, [
            {"trade_id": "a", "resolved_at": "2026-07-01T00:00:00", "pnl_usd": "2", "size_usd": "10"},
            {"trade_id": "c", "size_usd": "8", "resolution_date": "2026-07-05"},
        ])
        ledger = tmp_path / "live_wallet.json"
        L.reconcile_deposit(ledger, 100.0)                       # $100 real deposit
        monkeypatch.setattr(tb, "_live_trades_csv_path", lambda uid: csv_path)
        monkeypatch.setattr(tb, "_live_wallet_file", lambda uid: ledger)
        ws = tb.live_wallet_stats(1)
        assert ws["deposited"] == 100.0 and ws["realized_pnl"] == 2.0
        assert ws["deployed"] == 8.0
        assert ws["wallet_balance"] == 102.0 and ws["available"] == 94.0
        assert ws["return_pct"] == 2.0                            # 2 / 100


class TestModeDispatch:
    def test_status_live_no_trades(self, monkeypatch):
        monkeypatch.setattr(tb, "get_user_mode", lambda uid: "live")
        monkeypatch.setattr(tb, "read_live_stats", lambda uid: {})
        monkeypatch.setattr(tb, "_load_users", lambda: {1: {"went_live_at": "2026-07-03T00:00:00"}})
        out = tb.fmt_status(1)
        assert "LIVE" in out and "No live trades yet" in out

    def test_status_paper_path_used_when_paper(self, monkeypatch):
        called = {}

        def fake_paper(uid):
            called["p"] = True
            return "PAPER BODY"

        monkeypatch.setattr(tb, "get_user_mode", lambda uid: "paper")
        monkeypatch.setattr(tb, "_fmt_status_paper", fake_paper)
        assert tb.fmt_status(1) == "PAPER BODY" and called["p"]

    def test_wallet_live_shows_real_money(self, monkeypatch):
        monkeypatch.setattr(tb, "get_user_mode", lambda uid: "live")
        monkeypatch.setattr(tb, "live_wallet_stats", lambda uid: {
            "deposited": 100.0, "withdrawn": 0.0, "deployed": 8.0, "realized_pnl": 2.0,
            "wallet_balance": 102.0, "available": 94.0, "return_pct": 2.0,
            "pnl_today": 0.0, "pnl_week": 0.0, "pending_count": 1,
        })
        out = tb.fmt_wallet(1)
        assert "LIVE (real money)" in out and "real, on-chain" in out
