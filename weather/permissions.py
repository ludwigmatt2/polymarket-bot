"""
Role-based access control (RBAC) — the capability model for the Telegram bot.

This module is intentionally PURE: it imports nothing from telegram_bot and holds
no runtime state, so it is trivially unit-testable and cannot cause circular
imports. telegram_bot.py wires it to the live user store (get_role + per-user
permission overrides) via has_permission() / require_perm().

Roles (least → most privileged):
  suspended  — hard-blocked, no capabilities
  viewer     — read-only, paper only
  trader     — trades own funds (go live, deposit/withdraw own)
  admin      — operator: manage users, trigger scans, global config
  owner      — everything, incl. assigning admin/owner and withdraw_any

The primary admin (TELEGRAM_ADMIN_ID) is always seeded/kept as `owner`.
"""

from __future__ import annotations

# ── Capabilities ──────────────────────────────────────────────────────────────
# String constants so callers read as has_permission(uid, GO_LIVE).

VIEW                 = "view"                  # read own portfolio
GO_LIVE              = "go_live"               # switch own mode to live
DEPOSIT_OWN          = "deposit_own"           # record own deposit
WITHDRAW_OWN         = "withdraw_own"          # withdraw own funds
TRIGGER_SCAN         = "trigger_scan"          # run global scan/resolve via buttons
BYPASS_SCAN_COOLDOWN = "bypass_scan_cooldown"  # exempt from per-user scan cooldown
MANAGE_USERS         = "manage_users"          # add/remove/list/suspend users, invite
MANAGE_ROLES         = "manage_roles"          # change an existing user's role
SET_MAXBET           = "set_maxbet"            # global runtime max-bet config
USE_LEGACY_SETUP     = "use_legacy_setup"      # legacy /setup credential command
VIEW_USERS           = "view_users"            # list registered users
WITHDRAW_ANY         = "withdraw_any"          # withdraw from any user (owner; Phase C)

ALL_CAPABILITIES: frozenset[str] = frozenset({
    VIEW, GO_LIVE, DEPOSIT_OWN, WITHDRAW_OWN, TRIGGER_SCAN, BYPASS_SCAN_COOLDOWN,
    MANAGE_USERS, MANAGE_ROLES, SET_MAXBET, USE_LEGACY_SETUP, VIEW_USERS, WITHDRAW_ANY,
})

# ── Roles ─────────────────────────────────────────────────────────────────────

OWNER     = "owner"
ADMIN     = "admin"
TRADER    = "trader"
VIEWER    = "viewer"
SUSPENDED = "suspended"

ROLES: tuple[str, ...] = (OWNER, ADMIN, TRADER, VIEWER, SUSPENDED)

ROLE_CAPABILITIES: dict[str, frozenset[str]] = {
    OWNER: ALL_CAPABILITIES,
    ADMIN: frozenset({
        VIEW, GO_LIVE, DEPOSIT_OWN, WITHDRAW_OWN, TRIGGER_SCAN, BYPASS_SCAN_COOLDOWN,
        MANAGE_USERS, MANAGE_ROLES, SET_MAXBET, USE_LEGACY_SETUP, VIEW_USERS,
    }),  # NOTE: no WITHDRAW_ANY — only the owner can touch other users' funds.
    TRADER: frozenset({VIEW, GO_LIVE, DEPOSIT_OWN, WITHDRAW_OWN}),
    VIEWER: frozenset({VIEW}),
    SUSPENDED: frozenset(),
}

# Roles considered "authorized" to use the bot at all (everything but suspended /
# unknown). Order matters nowhere; membership is what's used.
AUTHORIZED_ROLES: frozenset[str] = frozenset({OWNER, ADMIN, TRADER, VIEWER})

# Roles that carry operator/admin privileges (the is_admin() shim).
ADMIN_ROLES: frozenset[str] = frozenset({OWNER, ADMIN})


# ── Pure predicates ─────────────────────────────────────────────────────────────

def is_valid_role(role: str | None) -> bool:
    return role in ROLES


def role_has(role: str | None, capability: str) -> bool:
    """Does this role grant `capability`? Unknown role → False."""
    return capability in ROLE_CAPABILITIES.get(role or "", frozenset())


def caps_for_role(role: str | None) -> frozenset[str]:
    return ROLE_CAPABILITIES.get(role or "", frozenset())


def assignable_roles(actor_role: str | None) -> frozenset[str]:
    """Which roles may an actor with `actor_role` grant to others?

    owner  → any role
    admin  → trader / viewer / suspended (cannot mint admin or owner)
    other  → nothing
    """
    if actor_role == OWNER:
        return frozenset(ROLES)
    if actor_role == ADMIN:
        return frozenset({TRADER, VIEWER, SUSPENDED})
    return frozenset()


def can_assign_role(actor_role: str | None, target_role: str) -> bool:
    """May an actor with `actor_role` assign `target_role` to someone?"""
    return is_valid_role(target_role) and target_role in assignable_roles(actor_role)
