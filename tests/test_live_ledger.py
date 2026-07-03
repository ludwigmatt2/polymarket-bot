"""PR2 — mode-aware /deposit + the real on-chain deposit ledger."""

import asyncio
import os
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("POLYMARKET_BOT_TOKEN", "test:token")
os.environ.setdefault("TELEGRAM_ADMIN_ID", "1")

from weather import live_ledger as L


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


class TestLiveLedger:
    def test_deposit_detection_and_idempotency(self, tmp_path):
        p = tmp_path / "live_wallet.json"
        assert L.reconcile_deposit(p, 10.0) == 10.0     # first deposit
        assert L.reconcile_deposit(p, 10.0) == 0.0      # same balance → nothing
        assert L.net_deposited(p) == 10.0

    def test_wrap_is_not_a_deposit(self, tmp_path):
        p = tmp_path / "live_wallet.json"
        L.reconcile_deposit(p, 10.0)
        assert L.reconcile_deposit(p, 0.0) == 0.0       # wrap to pUSD, not a deposit
        assert L.reconcile_deposit(p, 5.0) == 5.0       # a genuine new deposit
        assert L.net_deposited(p) == 15.0               # winnings never inflate this

    def test_sub_threshold_ignored(self, tmp_path):
        p = tmp_path / "live_wallet.json"
        assert L.reconcile_deposit(p, 0.005) == 0.0     # dust below min_delta
        assert L.net_deposited(p) == 0.0

    def test_explicit_withdraw_reduces_net(self, tmp_path):
        p = tmp_path / "live_wallet.json"
        L.reconcile_deposit(p, 100.0)
        L.record_transaction(p, "withdraw", 30.0, "cash out")
        assert L.net_deposited(p) == 70.0


def _msg_update(uid, args):
    update = MagicMock()
    update.effective_user.id = uid
    update.message.reply_text = AsyncMock()
    ctx = MagicMock()
    ctx.args = list(args)
    return update, ctx


class TestDepositCommand:
    def _tb(self, monkeypatch):
        import telegram_bot as tb
        monkeypatch.setattr(tb, "_ensure_authorized", AsyncMock(return_value=True))
        monkeypatch.setattr(tb, "has_permission", lambda *a, **k: True)
        return tb

    def test_paper_mode_labels_simulated(self, monkeypatch):
        tb = self._tb(monkeypatch)
        monkeypatch.setattr(tb, "get_user_mode", lambda uid: "paper")
        spy = MagicMock()
        monkeypatch.setattr(tb, "append_wallet_transaction", spy)
        monkeypatch.setattr(tb, "wallet_stats", lambda uid: {"deposited": 500.0, "wallet_balance": 500.0})
        update, ctx = _msg_update(7, ["500"])
        _run(lambda: tb.cmd_deposit(update, ctx))
        spy.assert_called_once()
        text = update.message.reply_text.call_args.args[0].lower()
        assert "paper" in text and "simulated" in text

    def test_live_mode_without_wallet(self, monkeypatch):
        tb = self._tb(monkeypatch)
        monkeypatch.setattr(tb, "get_user_mode", lambda uid: "live")
        monkeypatch.setattr("weather.secrets.get_user_creds", lambda uid: {})
        update, ctx = _msg_update(7, [])
        _run(lambda: tb.cmd_deposit(update, ctx))
        text = update.message.reply_text.call_args.args[0].lower()
        assert "wallet_setup" in text or "no deposit wallet" in text

    def test_live_mode_shows_address_and_records_deposit(self, monkeypatch, tmp_path):
        tb = self._tb(monkeypatch)
        monkeypatch.setattr(tb, "get_user_mode", lambda uid: "live")
        monkeypatch.setattr("weather.secrets.get_user_creds",
                            lambda uid: {"funder_address": "0xABC123", "signature_type": 3})
        monkeypatch.setattr("weather.relayer.usdce_balance", lambda w: 10.0)
        monkeypatch.setattr("weather.relayer.pusd_balance", lambda w: 2.0)
        ledger = tmp_path / "live_wallet.json"
        monkeypatch.setattr(tb, "_live_wallet_file", lambda uid: ledger)
        update, ctx = _msg_update(7, [])
        _run(lambda: tb.cmd_deposit(update, ctx))
        text = update.message.reply_text.call_args.args[0]
        assert "0xABC123" in text and "LIVE" in text
        assert L.net_deposited(ledger) == 10.0          # detected + recorded
