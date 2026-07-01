"""Tests for the telegram_bot withdrawal storage + rate limiter (Phase C wiring)."""

import os
from datetime import datetime, timedelta

import pytest


@pytest.fixture
def tb(monkeypatch):
    os.environ.setdefault("POLYMARKET_BOT_TOKEN", "test:token")
    os.environ.setdefault("TELEGRAM_ADMIN_ID", "1")
    import telegram_bot as _tb

    store: dict = {1: {"role": "owner"}, 2: {"role": "trader"}}
    monkeypatch.setattr(_tb, "_users_cache", store, raising=False)
    # Keep everything in memory — no disk writes.
    monkeypatch.setattr(_tb, "_save_users", lambda users: setattr(_tb, "_users_cache", users))
    monkeypatch.setattr(_tb, "_withdraw_attempts", {}, raising=False)
    return _tb


ADDR = "0x" + "a" * 40


def test_allowlist_add_get_remove(tb):
    tb.add_allowlist_entry(2, ADDR, "cold wallet")
    entries = tb.get_withdraw_allowlist(2)
    assert len(entries) == 1
    assert entries[0]["address"] == ADDR
    assert entries[0]["label"] == "cold wallet"
    assert "added_at" in entries[0]

    assert tb.remove_allowlist_entry(2, ADDR.upper()) is True   # case-insensitive
    assert tb.get_withdraw_allowlist(2) == []
    assert tb.remove_allowlist_entry(2, ADDR) is False          # already gone


def test_allowlist_add_is_idempotent_and_refreshes(tb):
    tb.add_allowlist_entry(2, ADDR, "first")
    tb.add_allowlist_entry(2, ADDR, "second")
    entries = tb.get_withdraw_allowlist(2)
    assert len(entries) == 1                # no duplicate
    assert entries[0]["label"] == "second"  # refreshed


def test_withdraw_cap_default_and_override(tb):
    from weather.config import WITHDRAW_DAILY_CAP_USD
    assert tb.get_withdraw_cap(2) == WITHDRAW_DAILY_CAP_USD
    tb.set_withdraw_cap(2, 123.0)
    assert tb.get_withdraw_cap(2) == 123.0


def test_withdrawn_today_sums_only_today(tb, monkeypatch):
    today = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
    yday = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
    monkeypatch.setattr(tb, "read_wallet", lambda uid: {"transactions": [
        {"type": "withdraw", "amount": 10, "timestamp": today},
        {"type": "withdraw", "amount": 5,  "timestamp": today},
        {"type": "withdraw", "amount": 99, "timestamp": yday},   # excluded
        {"type": "deposit",  "amount": 50, "timestamp": today},  # excluded (not withdraw)
    ]})
    assert tb.withdrawn_today(2) == pytest.approx(15.0)


def test_rate_limiter_trips_after_budget(tb):
    from weather.config import WITHDRAW_MAX_ATTEMPTS_PER_HR as N
    # First N attempts are allowed (return False), the next one trips (True).
    results = [tb.withdraw_rate_limited(2) for _ in range(N + 1)]
    assert results[:N] == [False] * N
    assert results[N] is True
    # Independent per user.
    assert tb.withdraw_rate_limited(1) is False
