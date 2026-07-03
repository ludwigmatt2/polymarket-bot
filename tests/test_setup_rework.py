"""PR4 — connect path derives a deposit wallet (no proxy step) + /exportkey."""

import asyncio
import os
import threading
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("POLYMARKET_BOT_TOKEN", "test:token")
os.environ.setdefault("TELEGRAM_ADMIN_ID", "1")

import telegram_onboarding as ob

# well-known anvil/hardhat test key — safe to embed
VALID_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"


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


class TestConnectPathRework:
    def test_proxy_state_removed(self):
        # Only WALLET_CHOICE, KEY_PASTE, FUNDING remain (no PROXY_PASTE).
        h = ob.build_conversation_handler()
        assert len(h.states) == 3
        assert not hasattr(ob, "on_proxy_paste")

    def test_key_paste_derives_deposit_wallet_and_goes_to_funding(self, monkeypatch):
        monkeypatch.setattr(ob, "set_user_creds", MagicMock())
        monkeypatch.setattr(ob, "_set_user_fields", MagicMock())
        enable = MagicMock(return_value={"funder_address": "0xDEP0517"})
        monkeypatch.setattr(ob, "enable_deposit_wallet", enable)

        update = MagicMock()
        update.effective_user.id = 55
        update.message.text = VALID_KEY
        update.message.delete = AsyncMock()
        update.effective_chat.send_message = AsyncMock()

        state = _run(lambda: ob.on_key_paste(update, MagicMock()))

        assert state == ob.FUNDING
        enable.assert_called_once()                         # deposit wallet derived
        sent = update.effective_chat.send_message.call_args.args[0]
        assert "0xDEP0517" in sent                          # funds go to deposit wallet


class TestExportKey:
    def _mk(self, monkeypatch):
        import telegram_bot as tb
        monkeypatch.setattr(tb, "_ensure_authorized", AsyncMock(return_value=True))
        return tb

    def test_no_key(self, monkeypatch):
        tb = self._mk(monkeypatch)
        monkeypatch.setattr("weather.secrets.get_user_creds", lambda uid: {})
        update = MagicMock()
        update.effective_user.id = 9
        update.effective_message.reply_text = AsyncMock()
        _run(lambda: tb.cmd_exportkey(update, MagicMock()))
        assert "no key stored" in update.effective_message.reply_text.call_args.args[0].lower()

    def test_reveals_audits_and_schedules_delete(self, monkeypatch):
        tb = self._mk(monkeypatch)
        monkeypatch.setattr("weather.secrets.get_user_creds",
                            lambda uid: {"pk": "0xSECRET", "funder_address": "0xDEP"})
        audit = MagicMock()
        monkeypatch.setattr(tb, "audit_log", audit)
        update = MagicMock()
        update.effective_user.id = 9
        update.effective_message.reply_text = AsyncMock(return_value=MagicMock(chat_id=1, message_id=2))
        ctx = MagicMock()
        _run(lambda: tb.cmd_exportkey(update, ctx))
        text = update.effective_message.reply_text.call_args.args[0]
        assert "0xSECRET" in text and "polymarket.com" in text
        audit.assert_called_once()
        assert audit.call_args.args[0] == "private_key_revealed"
        ctx.job_queue.run_once.assert_called_once()         # 60s auto-delete scheduled
