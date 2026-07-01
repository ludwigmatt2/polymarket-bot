"""
Withdrawal policy — the pure decision logic for SECURITY_PLAN Phase C.

No I/O and no telegram imports: telegram_bot supplies the stored allowlist, the
amount already withdrawn today, and the user's cap, and this module decides
whether a withdrawal is allowed. That keeps every rule unit-testable in isolation.

Defense model (assume the bot/session may be briefly compromised):
  - Funds can only leave to an address the user explicitly allowlisted.
  - A newly-allowlisted address is unusable until a cooling-off window passes
    (so an attacker can't add their address and drain in one shot).
  - Daily total is capped, bounding worst-case loss.
  - Large single withdrawals require a re-entered confirmation code.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .config import (
    WITHDRAW_COOLING_OFF_HOURS,
    WITHDRAW_DAILY_CAP_USD,
    WITHDRAW_LARGE_USD,
)

# Allowlist entry states
ABSENT  = "absent"
COOLING = "cooling"
ACTIVE  = "active"


def normalize_address(raw: str) -> str | None:
    """Canonical lowercase 0x-address, or None if not a 40-hex EVM address."""
    if not raw:
        return None
    s = raw.strip()
    if s[:2].lower() != "0x" or len(s) != 42:
        return None
    try:
        int(s[2:], 16)
    except ValueError:
        return None
    return s.lower()


def _parse_ts(ts: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None
    # Treat naive timestamps as UTC (the ledger writes naive utcnow()).
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def find_entry(allowlist: list[dict], address: str) -> dict | None:
    """The allowlist entry matching `address` (case-insensitive), or None."""
    target = normalize_address(address)
    if target is None:
        return None
    for e in allowlist:
        if normalize_address(e.get("address", "")) == target:
            return e
    return None


def entry_is_active(entry: dict, now: datetime,
                    cooling_hours: float = WITHDRAW_COOLING_OFF_HOURS) -> bool:
    """True once the entry's cooling-off window has elapsed."""
    added = _parse_ts(entry.get("added_at", ""))
    if added is None:
        return False  # unknown age → treat as still cooling (fail safe)
    return now - added >= timedelta(hours=cooling_hours)


def allowlist_state(allowlist: list[dict], address: str, now: datetime,
                    cooling_hours: float = WITHDRAW_COOLING_OFF_HOURS) -> str:
    """One of ABSENT / COOLING / ACTIVE for `address`."""
    entry = find_entry(allowlist, address)
    if entry is None:
        return ABSENT
    return ACTIVE if entry_is_active(entry, now, cooling_hours) else COOLING


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    requires_code: bool = False


def evaluate_withdrawal(
    amount: float,
    to_address: str,
    allowlist: list[dict],
    spent_today: float,
    cap: float = WITHDRAW_DAILY_CAP_USD,
    now: datetime | None = None,
    *,
    cooling_hours: float = WITHDRAW_COOLING_OFF_HOURS,
    large_threshold: float = WITHDRAW_LARGE_USD,
    code_ok: bool = False,
) -> Decision:
    """Decide whether a withdrawal of `amount` to `to_address` may proceed.

    `spent_today` is the sum of the user's withdrawals already made today; `cap`
    is their daily limit. `code_ok` is True when a valid large-withdrawal
    confirmation code has been supplied. Returns a Decision; when
    `requires_code` is set, the caller should issue/verify a code and retry.
    """
    now = now or datetime.now(timezone.utc)

    if normalize_address(to_address) is None:
        return Decision(False, "Invalid destination address.")
    if not (amount > 0) or amount != amount or amount in (float("inf"), float("-inf")):
        return Decision(False, "Amount must be a positive finite number.")

    state = allowlist_state(allowlist, to_address, now, cooling_hours)
    if state == ABSENT:
        return Decision(False,
                        "Destination is not allowlisted. Add it with /allowlist_add.")
    if state == COOLING:
        return Decision(False,
                        f"Address is in its {cooling_hours:.0f}h cooling-off window — "
                        "not yet usable for withdrawals.")

    if spent_today + amount > cap + 1e-9:
        remaining = max(0.0, cap - spent_today)
        return Decision(False,
                        f"Daily withdrawal cap ${cap:,.0f} would be exceeded "
                        f"(${spent_today:,.2f} already withdrawn today; "
                        f"${remaining:,.2f} left).")

    if amount >= large_threshold and not code_ok:
        return Decision(False,
                        f"Large withdrawal (≥ ${large_threshold:,.0f}) needs a "
                        "confirmation code.", requires_code=True)

    return Decision(True, "ok")
