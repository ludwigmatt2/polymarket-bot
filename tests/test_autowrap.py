"""Auto-wrap of new USDC.e deposits → pUSD for live users, so a top-up becomes
tradeable on the next scan without a manual /mymode re-flip."""
import os
from pathlib import Path

os.environ.setdefault("POLYMARKET_BOT_TOKEN", "test:token")
os.environ.setdefault("TELEGRAM_ADMIN_ID", "1")

import telegram_bot as tb


def _patch(monkeypatch, *, modes, creds, usdce, prep):
    monkeypatch.setattr(tb, "all_user_ids", lambda: list(modes))
    monkeypatch.setattr(tb, "get_user_mode", lambda uid: modes[uid])
    monkeypatch.setattr(tb, "_live_wallet_file", lambda uid: Path(f"/tmp/lw_{uid}.json"))
    import weather.secrets as sec
    import weather.relayer as rl
    import weather.live_ledger as ll
    monkeypatch.setattr(sec, "get_user_creds", lambda uid: creds.get(uid))
    monkeypatch.setattr(sec, "prepare_for_live", lambda uid: prep(uid))
    monkeypatch.setattr(rl, "usdce_balance", lambda w: usdce.get(w, 0.0))
    monkeypatch.setattr(ll, "reconcile_deposit", lambda path, amt, **k: amt)  # amt>0 → fresh


_W10 = "0xAAA0000000000000000000000000000000000010"
_W30 = "0xAAA0000000000000000000000000000000000030"


def test_wraps_only_live_users_with_funds(monkeypatch):
    _patch(
        monkeypatch,
        modes={10: "live", 20: "paper", 30: "live"},
        creds={10: {"signature_type": 3, "funder_address": _W10},
               20: {"signature_type": 3, "funder_address": "0xB"},   # paper → skipped
               30: {"signature_type": 3, "funder_address": _W30}},
        usdce={_W10: 5.0, _W30: 0.0},   # 10 has a new deposit; 30 has nothing to wrap
        prep=lambda uid: {"ready": True, "pusd": 5.0},
    )
    res = tb._wrap_pending_live_deposits()
    assert [r["uid"] for r in res] == [10]        # only the live, funded wallet acted
    assert res[0]["wrapped"] == 5.0 and res[0]["pusd"] == 5.0 and "error" not in res[0]


def test_skips_users_without_deposit_wallet(monkeypatch):
    _patch(
        monkeypatch,
        modes={10: "live"},
        creds={10: {"signature_type": 1, "funder_address": None}},   # legacy proxy, no sig=3
        usdce={_W10: 5.0},
        prep=lambda uid: {"ready": True, "pusd": 5.0},
    )
    assert tb._wrap_pending_live_deposits() == []


def test_surfaces_prep_error_and_isolates_failures(monkeypatch):
    def prep(uid):
        if uid == 10:
            return {"ready": False, "pusd": 0.0, "error": "wallet busy"}
        raise RuntimeError("boom")   # uid 30 blows up — must not block others

    _patch(
        monkeypatch,
        modes={10: "live", 30: "live"},
        creds={10: {"signature_type": 3, "funder_address": _W10},
               30: {"signature_type": 3, "funder_address": _W30}},
        usdce={_W10: 4.0, _W30: 9.0},
        prep=prep,
    )
    res = {r["uid"]: r for r in tb._wrap_pending_live_deposits()}
    assert res[10]["error"] == "wallet busy" and res[10]["fresh"] is True
    assert "boom" in res[30]["error"]   # per-user failure captured, not raised


def test_record_and_prepare_resets_watermark_so_next_deposit_counts_full(monkeypatch, tmp_path):
    """After wrapping drops USDC.e→0, the ledger watermark must reset to the post-wrap
    balance — else the NEXT deposit is measured from this deposit's level and
    under-counted. Uses the REAL ledger (not mocked) to exercise the watermark."""
    from weather import live_ledger as ll
    import weather.secrets as sec
    import weather.relayer as rl
    ledger = tmp_path / "lw.json"
    monkeypatch.setattr(tb, "_live_wallet_file", lambda uid: ledger)
    monkeypatch.setattr(sec, "prepare_for_live", lambda uid: {"ready": True, "pusd": 50.0})
    monkeypatch.setattr(rl, "usdce_balance", lambda w: 0.0)  # post-wrap balance ≈ 0

    res = tb._record_and_prepare(99, "0xFUND", 50.0)          # $50 lands, then wraps
    assert res["detected"] == 50.0
    assert ll.net_deposited(ledger) == 50.0
    assert ll.read_ledger(ledger)["last_usdce"] == 0.0        # watermark reset

    res2 = tb._record_and_prepare(99, "0xFUND", 30.0)         # a smaller 2nd deposit
    assert res2["detected"] == 30.0
    assert ll.net_deposited(ledger) == 80.0  # 50+30 in full — NOT missed as a "decrease"
