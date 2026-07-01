"""Tests for the withdrawal policy (weather/withdrawal_policy.py) — SECURITY_PLAN Phase C."""

from datetime import datetime, timedelta, timezone

import pytest

from weather import withdrawal_policy as wp

NOW = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)
ADDR = "0x" + "a" * 40
ADDR2 = "0x" + "b" * 40


def _entry(address=ADDR, hours_ago=48.0):
    return {"address": address,
            "added_at": (NOW - timedelta(hours=hours_ago)).isoformat()}


# ── normalize_address ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    (ADDR.upper(), ADDR),                 # case-folded
    ("  " + ADDR + "  ", ADDR),           # trimmed
    ("0x" + "A1" * 20, "0x" + "a1" * 20),
])
def test_normalize_valid(raw, expected):
    assert wp.normalize_address(raw) == expected


@pytest.mark.parametrize("raw", ["", None, "0x123", "abc" + "a" * 39,
                                 "0x" + "z" * 40, "0x" + "a" * 41])
def test_normalize_invalid(raw):
    assert wp.normalize_address(raw) is None


# ── allowlist state / cooling-off ──────────────────────────────────────────────

def test_state_absent():
    assert wp.allowlist_state([], ADDR, NOW) == wp.ABSENT
    assert wp.allowlist_state([_entry(ADDR2)], ADDR, NOW) == wp.ABSENT


def test_state_cooling_then_active():
    assert wp.allowlist_state([_entry(hours_ago=1)], ADDR, NOW) == wp.COOLING
    assert wp.allowlist_state([_entry(hours_ago=23.9)], ADDR, NOW) == wp.COOLING
    assert wp.allowlist_state([_entry(hours_ago=24)], ADDR, NOW) == wp.ACTIVE
    assert wp.allowlist_state([_entry(hours_ago=100)], ADDR, NOW) == wp.ACTIVE


def test_state_matches_case_insensitively():
    assert wp.allowlist_state([_entry(ADDR.upper())], ADDR.lower(), NOW) == wp.ACTIVE


def test_unparseable_added_at_is_cooling():
    assert wp.allowlist_state([{"address": ADDR, "added_at": "garbage"}], ADDR, NOW) == wp.COOLING


# ── evaluate_withdrawal ────────────────────────────────────────────────────────

def test_reject_invalid_address():
    d = wp.evaluate_withdrawal(10, "0xnope", [_entry()], 0, cap=500, now=NOW)
    assert not d.allowed and "address" in d.reason.lower()


@pytest.mark.parametrize("amount", [0, -5, float("inf"), float("nan")])
def test_reject_bad_amount(amount):
    d = wp.evaluate_withdrawal(amount, ADDR, [_entry()], 0, cap=500, now=NOW)
    assert not d.allowed


def test_reject_absent_address():
    d = wp.evaluate_withdrawal(10, ADDR, [], 0, cap=500, now=NOW)
    assert not d.allowed and "allowlist" in d.reason.lower()


def test_reject_cooling_address():
    d = wp.evaluate_withdrawal(10, ADDR, [_entry(hours_ago=2)], 0, cap=500, now=NOW)
    assert not d.allowed and "cooling" in d.reason.lower()


def test_reject_over_daily_cap():
    d = wp.evaluate_withdrawal(100, ADDR, [_entry()], spent_today=450, cap=500, now=NOW)
    assert not d.allowed and "cap" in d.reason.lower()


def test_allow_up_to_cap_exactly():
    d = wp.evaluate_withdrawal(50, ADDR, [_entry()], spent_today=450, cap=500, now=NOW)
    assert d.allowed


def test_large_requires_code_then_allows():
    big = wp.evaluate_withdrawal(300, ADDR, [_entry()], 0, cap=1000, now=NOW,
                                 large_threshold=250)
    assert not big.allowed and big.requires_code
    ok = wp.evaluate_withdrawal(300, ADDR, [_entry()], 0, cap=1000, now=NOW,
                                large_threshold=250, code_ok=True)
    assert ok.allowed


def test_happy_path_small_active_under_cap():
    d = wp.evaluate_withdrawal(10, ADDR, [_entry()], spent_today=0, cap=500, now=NOW)
    assert d.allowed and not d.requires_code


def test_cap_takes_precedence_over_code_prompt():
    # Over cap AND large → still rejected for cap, not merely prompted for a code.
    d = wp.evaluate_withdrawal(300, ADDR, [_entry()], spent_today=800, cap=1000, now=NOW,
                               large_threshold=250)
    assert not d.allowed and not d.requires_code and "cap" in d.reason.lower()
