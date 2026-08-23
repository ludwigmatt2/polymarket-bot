"""PR5 — command audit fixes: scan/resolve permission gate + mode-scoped views."""

import asyncio
import csv as _csv
import os
import threading
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("POLYMARKET_BOT_TOKEN", "test:token")
os.environ.setdefault("TELEGRAM_ADMIN_ID", "1")

import telegram_bot as tb


def _run(make_coro):
    box: dict = {}

    def runner():
        try:
            box["v"] = asyncio.run(make_coro())
        except BaseException as e:  # noqa: BLE001
            box["e"] = e

    t = threading.Thread(target=runner)
    t.start()
    t.join()
    if "e" in box:
        raise box["e"]
    return box["v"]


class TestActiveTradesPath:
    def test_follows_mode(self, monkeypatch):
        monkeypatch.setattr(tb, "get_user_mode", lambda uid: "live")
        assert tb._active_trades_csv_path(5) == tb._live_trades_csv_path(5)
        monkeypatch.setattr(tb, "get_user_mode", lambda uid: "paper")
        assert tb._active_trades_csv_path(5) == tb._trades_csv_path(5)


class TestScanPermissionGate:
    def _neutralize_auth(self, monkeypatch, has_perm):
        monkeypatch.setattr(tb, "_ensure_authorized", AsyncMock(return_value=True))
        monkeypatch.setattr(tb, "has_permission", lambda uid, cap: has_perm)
        monkeypatch.setattr(tb, "run_bot_async", AsyncMock(return_value=("", "", 0)))

    def test_scan_refused_without_trigger_scan(self, monkeypatch):
        self._neutralize_auth(monkeypatch, has_perm=False)
        update = MagicMock()
        update.effective_user.id = 5
        update.effective_message.reply_text = AsyncMock()
        _run(lambda: tb.cmd_scan(update, MagicMock()))
        tb.run_bot_async.assert_not_called()                 # scan never triggered
        assert "permission" in update.effective_message.reply_text.call_args.args[0].lower()

    def test_resolve_refused_without_trigger_scan(self, monkeypatch):
        self._neutralize_auth(monkeypatch, has_perm=False)
        update = MagicMock()
        update.effective_user.id = 5
        update.effective_message.reply_text = AsyncMock()
        _run(lambda: tb.cmd_resolve(update, MagicMock()))
        tb.run_bot_async.assert_not_called()


class TestScanOrderIssueSurfacing:
    """A live order can fail without failing the scan itself (rc=0) — the
    ORDER_ISSUE marker must reach the user even on a nominally successful
    /scan, not just get folded into the quiet '✅ Scan complete' text
    (Aug 23 2026: a correctly-sized live order vanished with zero trace)."""

    def _setup(self, monkeypatch, stdout):
        monkeypatch.setattr(tb, "_ensure_authorized", AsyncMock(return_value=True))
        monkeypatch.setattr(tb, "has_permission", lambda uid, cap: True)
        monkeypatch.setattr(tb, "run_bot_async", AsyncMock(return_value=(stdout, "", 0)))
        monkeypatch.setattr(tb, "get_user_mode", lambda uid: "live")
        update = MagicMock()
        update.effective_user.id = 5
        msg = MagicMock()
        msg.edit_text = AsyncMock()
        update.effective_message.reply_text = AsyncMock(return_value=msg)
        return update, msg

    def test_order_issue_surfaced_on_successful_scan(self, monkeypatch):
        stdout = ("5 evaluated | 1 actionable | 4 rejected\n"
                  "ORDER_ISSUE: Lowest temperature in Seoul | ValueError: bad signature")
        update, msg = self._setup(monkeypatch, stdout)
        _run(lambda: tb.cmd_scan(update, MagicMock()))
        text = msg.edit_text.call_args.args[0]
        assert "Live order issue" in text
        assert "bad signature" in text

    def test_no_issue_block_when_scan_clean(self, monkeypatch):
        stdout = "5 evaluated | 0 actionable | 5 rejected"
        update, msg = self._setup(monkeypatch, stdout)
        _run(lambda: tb.cmd_scan(update, MagicMock()))
        text = msg.edit_text.call_args.args[0]
        assert "Live order issue" not in text


class TestModeScopedViews:
    def test_trades_reads_live_and_badges_when_live(self, tmp_path, monkeypatch):
        # signal_time must be within the current era — _relevant_trades() (Aug
        # 20 2026 fix) scopes every listing view the same way wallet_stats() does.
        live_csv = tmp_path / "live_trades.csv"
        with open(live_csv, "w", newline="") as f:
            w = _csv.DictWriter(f, fieldnames=["trade_id", "market_title", "signal_time",
                                               "resolved_at", "pnl_usd"])
            w.writeheader()
            w.writerow({"trade_id": "L1", "market_title": "Live market",
                        "signal_time": "2026-08-10T00:00:00",
                        "resolved_at": "2026-08-10T00:00:00", "pnl_usd": "4"})
        monkeypatch.setattr(tb, "get_user_mode", lambda uid: "live")
        monkeypatch.setattr(tb, "_active_trades_csv_path", lambda uid: live_csv)
        out = tb.fmt_trades(9)
        assert "Live" in out and "Live market" in out

    def test_positions_excludes_errored_live_rows(self, tmp_path, monkeypatch):
        live_csv = tmp_path / "live_trades.csv"
        with open(live_csv, "w", newline="") as f:
            w = _csv.DictWriter(f, fieldnames=["market_title", "signal_time", "resolved_at",
                                               "size_usd", "error", "resolution_date", "edge_pp"])
            w.writeheader()
            w.writerow({"market_title": "Open ok", "signal_time": "2026-08-10T00:00:00",
                        "size_usd": "10", "error": "",
                        "resolution_date": "2026-07-09", "edge_pp": "0.2"})
            w.writerow({"market_title": "Errored", "signal_time": "2026-08-10T00:00:00",
                        "size_usd": "5",
                        "error": "insufficient balance", "resolution_date": "2026-07-09", "edge_pp": "0.2"})
        monkeypatch.setattr(tb, "_active_trades_csv_path", lambda uid: live_csv)
        out = tb.fmt_positions(9)
        assert "1 trades" in out          # only the non-errored position counts
        assert "$10 deployed" in out
