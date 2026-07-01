"""Tests for the RBAC model (weather/permissions.py) and the telegram_bot wiring
(has_permission + per-user overrides). See SECURITY_PLAN.md Phase B."""

import os

import pytest

from weather import permissions as perms

# ── Expected role → capability matrix (hardcoded, not derived from the module,
#    so a mistake in the module is actually caught) ──────────────────────────────

EXPECTED = {
    perms.OWNER: {
        perms.VIEW, perms.GO_LIVE, perms.DEPOSIT_OWN, perms.WITHDRAW_OWN,
        perms.TRIGGER_SCAN, perms.BYPASS_SCAN_COOLDOWN, perms.MANAGE_USERS,
        perms.MANAGE_ROLES, perms.SET_MAXBET, perms.USE_LEGACY_SETUP,
        perms.VIEW_USERS, perms.WITHDRAW_ANY,
    },
    perms.ADMIN: {
        perms.VIEW, perms.GO_LIVE, perms.DEPOSIT_OWN, perms.WITHDRAW_OWN,
        perms.TRIGGER_SCAN, perms.BYPASS_SCAN_COOLDOWN, perms.MANAGE_USERS,
        perms.MANAGE_ROLES, perms.SET_MAXBET, perms.USE_LEGACY_SETUP,
        perms.VIEW_USERS,  # NO withdraw_any
    },
    perms.TRADER: {perms.VIEW, perms.GO_LIVE, perms.DEPOSIT_OWN, perms.WITHDRAW_OWN},
    perms.VIEWER: {perms.VIEW},
    perms.SUSPENDED: set(),
}


@pytest.mark.parametrize("role", list(EXPECTED))
def test_role_capability_matrix(role):
    for cap in perms.ALL_CAPABILITIES:
        want = cap in EXPECTED[role]
        assert perms.role_has(role, cap) is want, f"{role} × {cap}"


def test_only_owner_has_withdraw_any():
    assert perms.role_has(perms.OWNER, perms.WITHDRAW_ANY)
    for role in (perms.ADMIN, perms.TRADER, perms.VIEWER, perms.SUSPENDED):
        assert not perms.role_has(role, perms.WITHDRAW_ANY)


def test_suspended_has_nothing():
    assert perms.caps_for_role(perms.SUSPENDED) == frozenset()
    for cap in perms.ALL_CAPABILITIES:
        assert not perms.role_has(perms.SUSPENDED, cap)


def test_unknown_role_has_nothing():
    for cap in perms.ALL_CAPABILITIES:
        assert not perms.role_has("nonsense", cap)
        assert not perms.role_has(None, cap)


def test_is_valid_role():
    for r in perms.ROLES:
        assert perms.is_valid_role(r)
    assert not perms.is_valid_role("nope")
    assert not perms.is_valid_role(None)


# ── Role-assignment guard rules ────────────────────────────────────────────────

def test_assignable_roles():
    assert perms.assignable_roles(perms.OWNER) == frozenset(perms.ROLES)
    assert perms.assignable_roles(perms.ADMIN) == frozenset(
        {perms.TRADER, perms.VIEWER, perms.SUSPENDED}
    )
    for r in (perms.TRADER, perms.VIEWER, perms.SUSPENDED, None):
        assert perms.assignable_roles(r) == frozenset()


def test_only_owner_can_mint_admin_or_owner():
    assert perms.can_assign_role(perms.OWNER, perms.ADMIN)
    assert perms.can_assign_role(perms.OWNER, perms.OWNER)
    assert not perms.can_assign_role(perms.ADMIN, perms.ADMIN)
    assert not perms.can_assign_role(perms.ADMIN, perms.OWNER)


def test_admin_can_assign_trader_viewer():
    assert perms.can_assign_role(perms.ADMIN, perms.TRADER)
    assert perms.can_assign_role(perms.ADMIN, perms.VIEWER)
    assert perms.can_assign_role(perms.ADMIN, perms.SUSPENDED)


def test_can_assign_rejects_invalid_role():
    assert not perms.can_assign_role(perms.OWNER, "wizard")
    assert not perms.can_assign_role(perms.TRADER, perms.VIEWER)


# ── telegram_bot wiring: has_permission + per-user overrides ────────────────────

@pytest.fixture
def tb(monkeypatch):
    # telegram_bot reads these at import time.
    os.environ.setdefault("POLYMARKET_BOT_TOKEN", "test:token")
    os.environ.setdefault("TELEGRAM_ADMIN_ID", "1")
    import telegram_bot as _tb

    users = {
        1: {"role": perms.OWNER},
        2: {"role": perms.ADMIN},
        3: {"role": perms.TRADER},
        4: {"role": perms.VIEWER},
        5: {"role": perms.SUSPENDED},
        6: {"role": perms.VIEWER, "permissions_override": [perms.GO_LIVE]},
        # 7: unknown user (not in store)
    }
    monkeypatch.setattr(_tb, "_users_cache", users, raising=False)
    return _tb


def test_has_permission_by_role(tb):
    assert tb.has_permission(1, perms.WITHDRAW_ANY)      # owner
    assert tb.has_permission(2, perms.MANAGE_USERS)      # admin
    assert not tb.has_permission(2, perms.WITHDRAW_ANY)  # admin lacks
    assert tb.has_permission(3, perms.GO_LIVE)           # trader
    assert not tb.has_permission(3, perms.MANAGE_USERS)  # trader lacks
    assert not tb.has_permission(4, perms.GO_LIVE)       # viewer lacks
    assert tb.has_permission(4, perms.VIEW)              # viewer views


def test_suspended_and_unknown_blocked(tb):
    for cap in perms.ALL_CAPABILITIES:
        assert not tb.has_permission(5, cap)  # suspended
        assert not tb.has_permission(7, cap)  # unknown uid


def test_permission_override_grants_extra_cap(tb):
    # uid 6 is a viewer with an explicit go_live override.
    assert tb.has_permission(6, perms.GO_LIVE)
    assert tb.has_permission(6, perms.VIEW)
    assert not tb.has_permission(6, perms.MANAGE_USERS)  # not granted


def test_is_admin_is_authorized(tb):
    assert tb.is_admin(1) and tb.is_admin(2)
    assert not tb.is_admin(3) and not tb.is_admin(4)
    assert tb.is_authorized(1) and tb.is_authorized(4)
    assert not tb.is_authorized(5)  # suspended
    assert not tb.is_authorized(7)  # unknown
