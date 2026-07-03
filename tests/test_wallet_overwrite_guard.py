"""PR1 — wallet safety: /wallet_setup re-entry + overwrite guards.

The dangerous path is ob_create (and /setup) silently regenerating over an
existing key, discarding the old wallet + its funds. These tests pin the guard.
"""

import asyncio
import os
import threading
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("POLYMARKET_BOT_TOKEN", "test:token")
os.environ.setdefault("TELEGRAM_ADMIN_ID", "1")

import telegram_onboarding as ob


def _run(make_coro):
    """Run a coroutine in a fresh thread+loop, isolated from any event loop other
    tests may have left running in the main thread (asyncio.run would then refuse)."""
    box: dict = {}

    def runner():
        try:
            box["v"] = asyncio.run(make_coro())
        except BaseException as e:  # noqa: BLE001 — re-raised on the calling thread
            box["e"] = e

    t = threading.Thread(target=runner)
    t.start()
    t.join()
    if "e" in box:
        raise box["e"]
    return box["v"]


def _cb_update(uid: int, data: str):
    q = AsyncMock()
    q.data = data
    update = MagicMock()
    update.callback_query = q
    update.effective_user.id = uid
    return update, q


def _callback_datas(markup) -> list:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


class TestConversationReentry:
    def test_allow_reentry_enabled(self):
        # Without this, /wallet_setup is silently dropped mid-conversation.
        assert ob.build_conversation_handler().allow_reentry is True


class TestCreateOverwriteGuard:
    def test_existing_key_asks_confirm_and_does_not_overwrite(self, monkeypatch):
        monkeypatch.setattr(ob, "get_user_creds",
                            lambda uid: {"pk": "0xabc", "funder_address": "0xDEAD"})
        gen = MagicMock()
        setc = MagicMock()
        monkeypatch.setattr(ob, "generate_wallet", gen)
        monkeypatch.setattr(ob, "set_user_creds", setc)

        update, q = _cb_update(111, "ob_create")
        state = _run(lambda: ob.on_wallet_choice(update, MagicMock()))

        assert state == ob.WALLET_CHOICE
        gen.assert_not_called()          # no new wallet generated
        setc.assert_not_called()         # existing key untouched
        markup = q.edit_message_text.call_args.kwargs["reply_markup"]
        cbs = _callback_datas(markup)
        assert "ob_create_confirm" in cbs and "ob_keep" in cbs

    def test_no_existing_key_proceeds_to_create(self, monkeypatch):
        monkeypatch.setattr(ob, "get_user_creds", lambda uid: None)
        did = {}

        async def fake_do(q, uid):
            did["uid"] = uid
            return ob.FUNDING

        monkeypatch.setattr(ob, "_do_create_wallet", fake_do)
        update, _ = _cb_update(222, "ob_create")
        state = _run(lambda: ob.on_wallet_choice(update, MagicMock()))
        assert state == ob.FUNDING and did["uid"] == 222

    def test_confirm_button_replaces(self, monkeypatch):
        did = {}

        async def fake_do(q, uid):
            did["uid"] = uid
            return ob.FUNDING

        monkeypatch.setattr(ob, "_do_create_wallet", fake_do)
        update, _ = _cb_update(333, "ob_create_confirm")
        state = _run(lambda: ob.on_wallet_choice(update, MagicMock()))
        assert state == ob.FUNDING and did["uid"] == 333

    def test_keep_current_goes_to_funding(self, monkeypatch):
        monkeypatch.setattr(ob, "get_user_creds",
                            lambda uid: {"pk": "0xabc", "funder_address": "0xBEEF"})
        update, q = _cb_update(444, "ob_keep")
        state = _run(lambda: ob.on_wallet_choice(update, MagicMock()))
        assert state == ob.FUNDING
        assert "0xBEEF" in q.edit_message_text.call_args.args[0]


class TestSetupOverwriteGuard:
    def _run_setup(self, monkeypatch, args, existing_key):
        import telegram_bot as tb
        monkeypatch.setattr(tb, "_ensure_authorized", AsyncMock(return_value=True))
        monkeypatch.setattr(tb, "has_permission", lambda *a, **k: True)
        monkeypatch.setattr(tb, "get_user_key", lambda uid: existing_key)
        setk = MagicMock()
        monkeypatch.setattr(tb, "set_user_key", setk)
        update = MagicMock()
        update.effective_user.id = 111
        update.message.reply_text = AsyncMock()
        ctx = MagicMock()
        ctx.args = list(args)
        _run(lambda: tb.cmd_setup(update, ctx))
        return update, setk

    def test_refuses_overwrite_without_replace(self, monkeypatch):
        update, setk = self._run_setup(monkeypatch, ["0xdeadbeef"], existing_key="0xexisting")
        setk.assert_not_called()
        assert "already have a wallet" in update.message.reply_text.call_args.args[0].lower()

    def test_replace_token_allows_overwrite(self, monkeypatch):
        # With `replace`, storage proceeds (set_user_key called).
        import telegram_bot as tb
        monkeypatch.setattr(tb, "derive_and_store_clob_creds", lambda uid: {})
        update, setk = self._run_setup(monkeypatch, ["0xdeadbeef", "replace"], existing_key="0xexisting")
        setk.assert_called_once()
