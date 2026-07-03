"""
Polymarket Bot — Telegram controller.

Multi-user, role-based:
  admin  — full control (scan, resolve, mode switch, user management)
  viewer — read-only (status, signals, trades)

Users stored in config/users.json. First admin seeded from TELEGRAM_ADMIN_ID in .env.

Push alerts fire when weather_bot.py writes logs/last_signals.json or
logs/last_resolved.json after each scheduled launchd run.

Strategy extensibility: each strategy module must expose scan() and get_stats().
The active strategy is selected via /strategy (admin only). Currently: weather.
"""

from __future__ import annotations

import asyncio
import csv
import json
import math
import os
import threading
from contextlib import contextmanager
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from weather._io import atomic_write_text
from weather.secrets import derive_and_store_clob_creds, get_user_key, set_user_creds, set_user_key
import telegram_views
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

ROOT          = Path(__file__).parent
from weather.paths import DATA_DIR
from weather import permissions as perms
from weather import withdrawal_policy as wpol
from weather.audit import audit_log, read_audit
from weather.config import (
    WITHDRAW_COOLING_OFF_HOURS,
    WITHDRAW_DAILY_CAP_USD,
    WITHDRAW_LARGE_USD,
    WITHDRAW_MAX_ATTEMPTS_PER_HR,
)
USERS_FILE    = DATA_DIR / "config" / "users.json"
WALLET_FILE   = DATA_DIR / "logs" / "wallet.json"

def user_log_dir(uid: int) -> Path:
    return DATA_DIR / "logs" / "users" / str(uid)

# The admin's data IS the root logs/ (the scan runs there; fan-out mirrors to
# everyone else). Non-admin users never fall back to root — a fresh user must
# see an empty slate, not the owner's portfolio. Every per-user file path derives
# from this one primitive, so the admin exception lives in exactly one place.
def user_data_dir(uid: int) -> Path:
    return DATA_DIR / "logs" if uid == ADMIN_ID else user_log_dir(uid)

def _trades_csv_path(uid: int) -> Path:
    return user_data_dir(uid) / "paper_trades.csv"

def _signals_path(uid: int) -> Path:
    return user_data_dir(uid) / "last_signals.json"

def _resolved_path(uid: int) -> Path:
    return user_data_dir(uid) / "last_resolved.json"


# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ["POLYMARKET_BOT_TOKEN"]
ADMIN_ID  = int(os.environ["TELEGRAM_ADMIN_ID"])
# PYTHON_BIN lets Railway (or Docker) point to the system Python; locally falls
# back to the venv.
PYTHON    = os.environ.get("PYTHON_BIN", str(ROOT / "venv" / "bin" / "python"))


# ── User registry ─────────────────────────────────────────────────────────────

_users_cache: dict[int, dict] | None = None
_users_lock = threading.Lock()

def _load_users() -> dict[int, dict]:
    global _users_cache
    if _users_cache is None:
        if not USERS_FILE.exists():
            _users_cache = {}
        else:
            _users_cache = {int(k): v for k, v in json.loads(USERS_FILE.read_text()).items()}
    return _users_cache

def _save_users(users: dict[int, dict]) -> None:
    global _users_cache
    USERS_FILE.parent.mkdir(exist_ok=True)
    atomic_write_text(USERS_FILE, json.dumps({str(k): v for k, v in users.items()}, indent=2), mode=0o600)
    _users_cache = users

@contextmanager
def _users_transaction():
    """Load, yield the mutable users dict, then save — all under the lock."""
    with _users_lock:
        users = _load_users()
        yield users
        _save_users(users)

def _seed_volume_data() -> None:
    """One-time: copy seed files from /app/_seed/ into DATA_DIR if they don't exist yet.

    _seed/ is bundled in the Docker image for the initial Railway deployment.
    Once data lives on the persistent volume this function is a no-op.
    """
    seed_dir = ROOT / "_seed"
    if not seed_dir.exists():
        return
    copied = 0
    for src in seed_dir.rglob("*"):
        if not src.is_file():
            continue
        rel  = src.relative_to(seed_dir)
        dest = DATA_DIR / rel
        if dest.exists():
            continue
        size = src.stat().st_size
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(src.read_bytes())
        print(f"[seed] {dest} ({size:,} bytes)", flush=True)
        copied += 1
    if copied:
        print(f"[seed] Migration complete — {copied} file(s) written to {DATA_DIR}", flush=True)


def _seed_admin() -> None:
    """Ensure the primary admin (TELEGRAM_ADMIN_ID) exists and is always `owner`.

    Owner is the only role that can mint other admins/owners, so the bootstrap
    account must hold it — including upgrading a legacy `admin` seed in place."""
    with _users_lock:
        users = _load_users()
        if ADMIN_ID not in users:
            users[ADMIN_ID] = {
                "role": perms.OWNER,
                "username": "owner",
                "added_at": datetime.utcnow().isoformat(),
                "private_key": None,
                "proxy_address": None,
                "mode": "paper",
            }
            _save_users(users)
        elif users[ADMIN_ID].get("role") != perms.OWNER:
            users[ADMIN_ID]["role"] = perms.OWNER  # migrate legacy admin → owner
            _save_users(users)


def _seed_admin_creds() -> None:
    """One-time: import admin credentials from env vars into the encrypted store.

    Runs on every startup but is a no-op once credentials are already stored.
    Keeps POLYMARKET_PRIVATE_KEY / POLYMARKET_PROXY_ADDRESS as Railway env vars
    for the initial bootstrap; after the first successful seed they are no longer
    read at runtime (the encrypted store takes over).
    """
    from weather.secrets import get_user_creds, set_user_creds
    if get_user_creds(ADMIN_ID):
        return  # already stored — nothing to do
    pk    = os.environ.get("POLYMARKET_PRIVATE_KEY", "").strip()
    proxy = os.environ.get("POLYMARKET_PROXY_ADDRESS", "").strip()
    if not pk:
        return  # env vars not set yet — skip silently
    sig = "gnosis-safe" if proxy else "eoa"
    set_user_creds(ADMIN_ID, pk=pk, proxy_address=proxy or None, signature_type=sig)
    print(f"[startup] Admin credentials seeded from env vars into encrypted store (sig={sig}).", flush=True)

def get_user_mode(uid: int) -> str:
    return _load_users().get(uid, {}).get("mode", "paper")

def get_user_private_key(uid: int) -> Optional[str]:
    return _load_users().get(uid, {}).get("private_key")

def get_role(user_id: int) -> Optional[str]:
    return _load_users().get(user_id, {}).get("role")

def get_permission_overrides(user_id: int) -> list[str]:
    """Per-user extra capabilities granted beyond their role (see users.json)."""
    return _load_users().get(user_id, {}).get("permissions_override") or []

def is_admin(user_id: int) -> bool:
    return get_role(user_id) in perms.ADMIN_ROLES

def is_authorized(user_id: int) -> bool:
    return get_role(user_id) in perms.AUTHORIZED_ROLES

def has_permission(user_id: int, capability: str) -> bool:
    """True if the user's role grants `capability`, or it's in their per-user
    permissions_override list. Suspended/unknown users have no capabilities."""
    if perms.role_has(get_role(user_id), capability):
        return True
    return capability in get_permission_overrides(user_id)

def all_user_ids() -> list[int]:
    return list(_load_users().keys())


# ── Owner notifications (audit F2) ─────────────────────────────────────────────

async def notify_owner(bot, text: str) -> None:
    """Best-effort Telegram DM to the owner (ADMIN_ID). Never raises."""
    try:
        await bot.send_message(ADMIN_ID, text, parse_mode="Markdown")
    except Exception:
        pass

# Throttle repeated alerts of the same kind per uid (seconds since last send).
_alert_last: dict[tuple, float] = {}

def _alert_throttled(key: tuple, window: float = 3600.0) -> bool:
    """True if an alert with this key fired within `window` (and should be skipped)."""
    now = _time.monotonic()
    last = _alert_last.get(key, 0.0)
    if now - last < window:
        return True
    _alert_last[key] = now
    return False


async def _maybe_alert_suspended(update, ctx) -> None:
    uid = update.effective_user.id
    if get_role(uid) == perms.SUSPENDED and not _alert_throttled(("suspended", uid)):
        audit_log("suspended_access_attempt", actor=uid)
        await notify_owner(ctx.bot, f"⚠️ Suspended user `{uid}` attempted an action.")


# ── Auth decorators ────────────────────────────────────────────────────────────

async def _ensure_authorized(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    """Shared gate: True if the user may use the bot at all; else alerts on a
    suspended attempt, replies with the not-authorized notice, and returns False."""
    if is_authorized(update.effective_user.id):
        return True
    await _maybe_alert_suspended(update, ctx)
    await update.effective_message.reply_text(
        "🚫 Not authorized. Ask an admin to add you with /adduser."
    )
    return False


def require_auth():
    def decorator(fn):
        async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
            if not await _ensure_authorized(update, ctx):
                return
            return await fn(update, ctx)
        return wrapper
    return decorator


def require_perm(capability: str):
    """Gate a handler on a single RBAC capability. Authorized-but-lacking → a
    friendly refusal; unauthorized/suspended → the standard not-authorized notice."""
    def decorator(fn):
        async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
            if not await _ensure_authorized(update, ctx):
                return
            if not has_permission(update.effective_user.id, capability):
                await update.effective_message.reply_text(
                    "🚫 You don't have permission to do that."
                )
                return
            return await fn(update, ctx)
        return wrapper
    return decorator


# ── Wallet (per-user ledger; audit C3) ───────────────────────────────────────

def user_wallet_file(uid: int) -> Path:
    return user_log_dir(uid) / "wallet.json"

def _live_wallet_file(uid: int) -> Path:
    """Real on-chain deposit ledger — sits beside the matching live_trades.csv."""
    return user_data_dir(uid) / "live_wallet.json"

def _live_trades_csv_path(uid: int) -> Path:
    """Real live-order log — matches where weather_bot writes live_trades.csv."""
    return user_data_dir(uid) / "live_trades.csv"

def _active_trades_csv_path(uid: int) -> Path:
    """The trade log for the user's CURRENT mode — live orders when live, paper
    otherwise. Used by the 'current view' commands (/positions, /trades, /losses,
    /why) so a live account sees its real trades, not the paper mirror. Paper-stat
    readers (read_stats, wallet_stats) keep using _trades_csv_path unconditionally."""
    return _live_trades_csv_path(uid) if get_user_mode(uid) == "live" else _trades_csv_path(uid)

def _migrate_global_wallet() -> None:
    """One-time: move the legacy global ledger to the admin's per-user file."""
    admin_wallet = user_wallet_file(ADMIN_ID)
    if WALLET_FILE.exists() and not admin_wallet.exists():
        admin_wallet.parent.mkdir(parents=True, exist_ok=True)
        admin_wallet.write_text(WALLET_FILE.read_text())
        WALLET_FILE.rename(WALLET_FILE.with_suffix(".json.pre_per_user_migration"))

def read_wallet(uid: int) -> dict:
    f = user_wallet_file(uid)
    if not f.exists():
        return {"transactions": []}
    return json.loads(f.read_text())

def append_wallet_transaction(uid: int, tx_type: str, amount: float, note: str = "") -> None:
    data = read_wallet(uid)
    data["transactions"].append({
        "type": tx_type,
        "amount": amount,
        "timestamp": datetime.utcnow().isoformat(),
        "note": note,
    })
    f = user_wallet_file(uid)
    f.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(f, json.dumps(data, indent=2), mode=0o600)

def wallet_stats(uid: int) -> dict:
    txns = read_wallet(uid).get("transactions", [])
    deposited = sum(t["amount"] for t in txns if t["type"] == "deposit")
    withdrawn = sum(t["amount"] for t in txns if t["type"] == "withdraw")

    csv_path = _trades_csv_path(uid)
    rows: list[dict] = []
    if csv_path.exists():
        with csv_path.open() as f:
            rows = list(csv.DictReader(f))

    resolved = [r for r in rows if r.get("resolved_at")]
    pending  = [r for r in rows if not r.get("resolved_at")]

    realized_pnl = sum(float(r["pnl_usd"]) for r in resolved if r.get("pnl_usd"))
    deployed     = sum(float(r["size_usd"]) for r in pending  if r.get("size_usd"))

    wallet_balance = deposited - withdrawn + realized_pnl
    available      = wallet_balance - deployed
    return_pct     = (realized_pnl / deposited * 100) if deposited > 0 else None

    today_str  = datetime.utcnow().strftime("%Y-%m-%d")
    week_start = (datetime.utcnow() - timedelta(days=7)).isoformat()

    pnl_today = sum(
        float(r["pnl_usd"]) for r in resolved
        if r.get("pnl_usd") and r.get("resolved_at", "")[:10] == today_str
    )
    pnl_week = sum(
        float(r["pnl_usd"]) for r in resolved
        if r.get("pnl_usd") and r.get("resolved_at", "") >= week_start
    )

    return {
        "deposited":     deposited,
        "withdrawn":     withdrawn,
        "deployed":      deployed,
        "realized_pnl":  realized_pnl,
        "wallet_balance": wallet_balance,
        "available":     available,
        "return_pct":    return_pct,
        "pnl_today":     pnl_today,
        "pnl_week":      pnl_week,
        "resolved_count": len(resolved),
        "pending_count":  len(pending),
    }


# ── Withdrawal hardening (audit C-Phase; SECURITY_PLAN Phase C) ────────────────

def get_withdraw_allowlist(uid: int) -> list[dict]:
    return _load_users().get(uid, {}).get("withdraw_allowlist") or []

def add_allowlist_entry(uid: int, address: str, label: str = "") -> None:
    """Add (or refresh the cooling-off clock of) an allowlisted destination."""
    canon = wpol.normalize_address(address)
    with _users_transaction() as users:
        u = users.setdefault(uid, {})
        entries = [e for e in (u.get("withdraw_allowlist") or [])
                   if wpol.normalize_address(e.get("address", "")) != canon]
        entries.append({
            "address": canon,
            "label": label,
            "added_at": datetime.utcnow().isoformat(),
        })
        u["withdraw_allowlist"] = entries

def remove_allowlist_entry(uid: int, address: str) -> bool:
    canon = wpol.normalize_address(address)
    removed = False
    with _users_transaction() as users:
        u = users.get(uid)
        if not u:
            return False
        before = u.get("withdraw_allowlist") or []
        after = [e for e in before if wpol.normalize_address(e.get("address", "")) != canon]
        removed = len(after) != len(before)
        u["withdraw_allowlist"] = after
    return removed

def get_withdraw_cap(uid: int) -> float:
    cap = _load_users().get(uid, {}).get("withdraw_cap_usd")
    return float(cap) if cap is not None else WITHDRAW_DAILY_CAP_USD

def set_withdraw_cap(uid: int, usd: float) -> None:
    with _users_transaction() as users:
        users.setdefault(uid, {})["withdraw_cap_usd"] = float(usd)

def withdrawn_today(uid: int) -> float:
    """Sum of the user's ledger withdrawals dated today (UTC)."""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    total = 0.0
    for t in read_wallet(uid).get("transactions", []):
        if t.get("type") == "withdraw" and str(t.get("timestamp", ""))[:10] == today:
            total += float(t.get("amount") or 0)
    return total


# Rate limiter — per-uid withdrawal attempt timestamps (monotonic seconds).
_withdraw_attempts: dict[int, list[float]] = {}

def withdraw_rate_limited(uid: int) -> bool:
    """Record an attempt; True if the user is over the hourly attempt budget."""
    now = _time.monotonic()
    recent = [t for t in _withdraw_attempts.get(uid, []) if now - t < 3600]
    recent.append(now)
    _withdraw_attempts[uid] = recent
    return len(recent) > WITHDRAW_MAX_ATTEMPTS_PER_HR


# ── Stats ─────────────────────────────────────────────────────────────────────

def read_stats(uid: int) -> dict:
    trades_csv = _trades_csv_path(uid)
    if not trades_csv.exists():
        return {}
    with trades_csv.open() as f:
        rows = list(csv.DictReader(f))

    resolved = [r for r in rows if r.get("resolved_at")]
    wins   = [r for r in resolved if r.get("pnl_usd") and float(r["pnl_usd"]) > 0]
    losses = [r for r in resolved if r.get("pnl_usd") and float(r["pnl_usd"]) < 0]

    gross_win  = sum(float(r["pnl_usd"]) for r in wins)
    gross_loss = abs(sum(float(r["pnl_usd"]) for r in losses))
    pf         = gross_win / gross_loss if gross_loss > 0 else None
    total_pnl  = sum(float(r["pnl_usd"]) for r in resolved if r.get("pnl_usd"))

    brier      = [float(r["brier_score"]) for r in resolved if r.get("brier_score")]
    mean_brier = sum(brier) / len(brier) if brier else None
    bss        = 1 - (mean_brier / 0.25) if mean_brier else None

    pending_by_date: dict[str, int] = {}
    for r in rows:
        if not r.get("resolved_at") and r.get("resolution_date"):
            d = r["resolution_date"][:10]
            pending_by_date[d] = pending_by_date.get(d, 0) + 1

    return {
        "total": len(rows),
        "resolved": len(resolved),
        "pending": len(rows) - len(resolved),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(resolved) * 100 if resolved else None,
        "profit_factor": pf,
        "total_pnl": total_pnl,
        "mean_brier": mean_brier,
        "bss": bss,
        "pending_by_date": dict(sorted(pending_by_date.items())),
        "gates_passed": len(resolved) >= 20 and pf is not None and pf >= 1.5,
    }

def read_live_stats(uid: int) -> dict:
    """Real-money stats from live_trades.csv — the clean slate that begins at
    go-live. Errored rows (order never placed) are excluded."""
    csv_path = _live_trades_csv_path(uid)
    if not csv_path.exists():
        return {}
    with csv_path.open() as f:
        rows = [r for r in csv.DictReader(f) if not r.get("error")]

    resolved = [r for r in rows if r.get("resolved_at")]
    wins   = [r for r in resolved if r.get("pnl_usd") and float(r["pnl_usd"]) > 0]
    losses = [r for r in resolved if r.get("pnl_usd") and float(r["pnl_usd"]) < 0]
    gross_win  = sum(float(r["pnl_usd"]) for r in wins)
    gross_loss = abs(sum(float(r["pnl_usd"]) for r in losses))
    pf         = gross_win / gross_loss if gross_loss > 0 else None
    total_pnl  = sum(float(r["pnl_usd"]) for r in resolved if r.get("pnl_usd"))
    deployed   = sum(float(r["size_usd"]) for r in rows
                     if not r.get("resolved_at") and r.get("size_usd"))
    today_str  = datetime.utcnow().strftime("%Y-%m-%d")
    week_start = (datetime.utcnow() - timedelta(days=7)).isoformat()
    pnl_today  = sum(float(r["pnl_usd"]) for r in resolved
                     if r.get("pnl_usd") and r.get("resolved_at", "")[:10] == today_str)
    pnl_week   = sum(float(r["pnl_usd"]) for r in resolved
                     if r.get("pnl_usd") and r.get("resolved_at", "") >= week_start)
    return {
        "total": len(rows),
        "resolved": len(resolved),
        "pending": len(rows) - len(resolved),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(resolved) * 100 if resolved else None,
        "profit_factor": pf,
        "total_pnl": total_pnl,
        "deployed": deployed,
        "pnl_today": pnl_today,
        "pnl_week": pnl_week,
    }

def live_wallet_stats(uid: int, ls: dict | None = None) -> dict:
    """Wallet view from REAL money: net on-chain deposits + realized live PnL.
    Return % is computed from actual capital invested, not simulated paper deposits.
    Pass `ls` (a read_live_stats result) to avoid re-parsing live_trades.csv."""
    from weather import live_ledger
    t   = live_ledger.totals(_live_wallet_file(uid))
    ls  = ls if ls is not None else (read_live_stats(uid) or {})
    realized = ls.get("total_pnl", 0.0)
    deployed = ls.get("deployed", 0.0)
    deposited, withdrawn, net = t["deposited"], t["withdrawn"], t["net"]

    # Accounting balance (deposits − withdrawals + realized PnL). The on-chain pUSD
    # is the source of truth shown by /deposit; this is the ROI ledger view.
    balance    = net + realized
    available  = balance - deployed
    return_pct = (realized / deposited * 100) if deposited > 0 else None
    return {
        "deposited": deposited, "withdrawn": withdrawn,
        "deployed": deployed, "realized_pnl": realized,
        "wallet_balance": balance, "available": available,
        "return_pct": return_pct,
        "pnl_today": ls.get("pnl_today", 0.0), "pnl_week": ls.get("pnl_week", 0.0),
        "pending_count": ls.get("pending", 0),
    }

def read_last_signals(uid: int) -> list[dict]:
    f = _signals_path(uid)
    if not f.exists():
        return []
    return json.loads(f.read_text()).get("signals", [])

def read_last_scan_meta(uid: int) -> dict:
    f = _signals_path(uid)
    if not f.exists():
        return {}
    try:
        data    = json.loads(f.read_text())
        signals = data.get("signals", [])
        return {
            "scanned_at": data.get("scanned_at", "")[:16].replace("T", " "),
            "total":      len(signals),
            "high_conf":  sum(1 for s in signals if s.get("edge_pp", 0) >= 0.30),
        }
    except Exception:
        return {}

def get_mode(uid: int) -> str:
    mode = get_user_mode(uid)
    return "🟢 LIVE" if mode == "live" else "🟡 PAPER"


# ── City extractor ────────────────────────────────────────────────────────────

_CITY_MAP = [
    ("HK",    ["Hong Kong"]),
    ("SZ",    ["Shenzhen"]),
    ("NYC",   ["NYC", "New York City", "New York"]),
    ("LON",   ["London"]),
    ("SEA",   ["Seattle"]),
    ("SEO",   ["Seoul"]),
    ("TA",    ["Tel Aviv"]),
    ("HNL",   ["Honolulu"]),
    ("SYD",   ["Sydney"]),
    ("TYO",   ["Tokyo"]),
    ("MIA",   ["Miami"]),
    ("DAL",   ["Dallas"]),
    ("ATL",   ["Atlanta"]),
    ("MAD",   ["Madrid"]),
    ("CHI",   ["Chicago"]),
    ("LAX",   ["Los Angeles"]),
    ("SFO",   ["San Francisco"]),
    ("BER",   ["Berlin"]),
    ("PAR",   ["Paris"]),
    ("SGP",   ["Singapore"]),
]

def _extract_city(title: str) -> str:
    t = title.lower()
    for abbr, patterns in _CITY_MAP:
        if any(p.lower() in t for p in patterns):
            return abbr
    return "?"


# ── Formatters ────────────────────────────────────────────────────────────────

def fmt_status(uid: int) -> str:
    """Live accounts see ONLY their real-money record (clean slate from go-live);
    paper accounts see the paper record + go-live gate. Paper data is never lost —
    it keeps feeding calibration and is viewable via /paperstats."""
    if get_user_mode(uid) == "live":
        return _fmt_status_live(uid)
    return _fmt_status_paper(uid)


def _fmt_status_live(uid: int) -> str:
    ls   = read_live_stats(uid)
    ws   = live_wallet_stats(uid, ls)   # reuse the parse ls already did
    scan = read_last_scan_meta(uid)
    since = _load_users().get(uid, {}).get("went_live_at", "")[:10]
    header = "*📊 Status* — 🟢 LIVE" + (f"  _(since {since})_" if since else "")

    if not ls:
        return (header + "\n\nNo live trades yet — real orders execute on the next "
                "scheduled scan.\nFund with /deposit.")

    lines = [header + "\n"]
    if ws["return_pct"] is not None:
        sign = "+" if ws["return_pct"] >= 0 else ""
        lines.append(f"📈 *{sign}{ws['return_pct']:.1f}% return*"
                     f"   (${ws['realized_pnl']:+,.2f} on ${ws['deposited']:,.2f} real)")
    else:
        lines.append(f"📈 Realized PnL: *${ws['realized_pnl']:+,.2f}*"
                     f"   _(fund via /deposit to track ROI %)_")
    lines.append(f"\n{ls['resolved']} resolved · {ls['pending']} open · {ls['total']} total")
    if ls["win_rate"] is not None:
        lines.append(f"Win rate:      {ls['win_rate']:.1f}%  ({ls['wins']}W / {ls['losses']}L)")
    if ls["profit_factor"] is not None:
        lines.append(f"Prof. factor:  {ls['profit_factor']:.2f}")
    lines.append(f"Deployed now:  ${ws['deployed']:,.2f}  ·  Available ${ws['available']:,.2f}")
    if scan:
        lines.append(f"\nLast scan: {scan['scanned_at']} UTC"
                     f" · {scan['high_conf']} high-conf / {scan['total']} signals")
    lines.append("\n_Paper/model record: /paperstats_")
    return "\n".join(lines)


def _fmt_status_paper(uid: int) -> str:
    s    = read_stats(uid)
    ws   = wallet_stats(uid)
    mode = get_mode(uid)
    scan = read_last_scan_meta(uid)

    if not s:
        header = f"*📊 Status* — {mode}\n"
        if scan:
            return header + f"Last scan: {scan['scanned_at']} UTC\nNo trades yet."
        return header + "No data yet. Run /scan to start."

    lines = [f"*📊 Status* — {mode}\n"]

    # Lead with return
    if ws["return_pct"] is not None:
        sign = "+" if ws["return_pct"] >= 0 else ""
        lines.append(
            f"📈 *{sign}{ws['return_pct']:.1f}% return*"
            f"   (${ws['realized_pnl']:+,.2f} on ${ws['deposited']:,.0f})"
        )
    else:
        lines.append(
            f"📈 PnL: *${s['total_pnl']:+,.2f}*"
            f"   _(use /deposit to track ROI %)_"
        )

    lines.append(f"\n{s['resolved']} resolved · {s['pending']} pending · {s['total']} total")

    if s["win_rate"] is not None:
        lines.append(f"Win rate:      {s['win_rate']:.1f}%  ({s['wins']}W / {s['losses']}L)")
    if s["profit_factor"] is not None:
        e = "✅" if s["profit_factor"] >= 1.5 else "⚠️"
        lines.append(f"Prof. factor:  {e} {s['profit_factor']:.2f}  (need ≥ 1.5)")
    if s["bss"] is not None:
        e = "✅" if s["bss"] >= 0 else "⚠️"
        lines.append(f"Brier skill:   {e} {s['bss']:+.3f}  (need ≥ 0.0)")

    if scan:
        lines.append(
            f"\nLast scan: {scan['scanned_at']} UTC"
            f" · {scan['high_conf']} high-conf / {scan['total']} signals"
        )

    # Go-live gate
    all_pass = (
        s["resolved"] >= 20
        and s["profit_factor"] is not None and s["profit_factor"] >= 1.5
        and s["bss"] is not None and s["bss"] >= 0.0
    )
    if all_pass:
        lines.append("\n🟢 *All gates passed — ready for live*")
    else:
        missing = []
        if s["resolved"] < 20:
            missing.append(f"{s['resolved']}/20 trades")
        if s["profit_factor"] is None or s["profit_factor"] < 1.5:
            pf_str = f"{s['profit_factor']:.2f}" if s["profit_factor"] else "N/A"
            missing.append(f"PF {pf_str}")
        if s["bss"] is None or s["bss"] < 0.0:
            bss_str = f"{s['bss']:+.3f}" if s["bss"] is not None else "N/A"
            missing.append(f"BSS {bss_str}")
        lines.append(f"\n🔴 Gates pending: {', '.join(missing)}")

    return "\n".join(lines)


def fmt_wallet(uid: int) -> str:
    if get_user_mode(uid) == "live":
        return _fmt_wallet_live(uid)
    return _fmt_wallet_paper(uid)


def _fmt_wallet_live(uid: int) -> str:
    ws = live_wallet_stats(uid)
    lines = ["*💼 Wallet* — 🟢 LIVE (real money)\n"]
    if ws["deposited"] == 0:
        lines.append("No real deposits yet.")
        lines.append("Use /deposit to see your funding address + on-chain balance.\n")
        lines.append(f"Realized PnL (live):  *${ws['realized_pnl']:+,.2f}*")
        lines.append(f"Deployed right now:   ${ws['deployed']:,.2f}")
        return "\n".join(lines)
    ret_str = ""
    if ws["return_pct"] is not None:
        sign = "+" if ws["return_pct"] >= 0 else ""
        ret_str = f"  ({sign}{ws['return_pct']:.1f}%)"
    lines.append(f"💰 Balance:   *${ws['wallet_balance']:,.2f}*{ret_str}")
    lines.append(f"├─ Deposited  ${ws['deposited']:,.2f}  _(real, on-chain)_")
    if ws["withdrawn"] > 0:
        lines.append(f"├─ Withdrawn  -${ws['withdrawn']:,.2f}")
    lines.append(f"├─ Deployed   ${ws['deployed']:,.2f}  ({ws['pending_count']} open positions)")
    avail_sign = "+" if ws["available"] >= 0 else ""
    lines.append(f"└─ Available  {avail_sign}${ws['available']:,.2f}")
    lines.append(f"\n📊 *Realized PnL (live)*")
    lines.append(f"All-time:  *${ws['realized_pnl']:+,.2f}*")
    if abs(ws["pnl_week"]) > 0:
        lines.append(f"This week: ${ws['pnl_week']:+,.2f}")
    if abs(ws["pnl_today"]) > 0:
        lines.append(f"Today:     ${ws['pnl_today']:+,.2f}")
    lines.append(f"\n_On-chain balance: /deposit · Paper record: /paperstats_")
    return "\n".join(lines)


def _fmt_wallet_paper(uid: int) -> str:
    ws   = wallet_stats(uid)
    mode = get_mode(uid)
    txns = read_wallet(uid).get("transactions", [])

    lines = [f"*💼 Wallet* — {mode}\n"]

    if ws["deposited"] == 0:
        lines.append("No deposits recorded yet.")
        lines.append("Use /deposit <amount> to start tracking your wallet.\n")
        lines.append(f"Realized PnL (paper):  *${ws['realized_pnl']:+,.2f}*")
        lines.append(f"Deployed right now:    ${ws['deployed']:,.2f}")
        return "\n".join(lines)

    # Balance block
    ret_str = ""
    if ws["return_pct"] is not None:
        sign = "+" if ws["return_pct"] >= 0 else ""
        ret_str = f"  ({sign}{ws['return_pct']:.1f}%)"
    lines.append(f"💰 Balance:   *${ws['wallet_balance']:,.2f}*{ret_str}")
    lines.append(f"├─ Deposited  ${ws['deposited']:,.2f}")
    if ws["withdrawn"] > 0:
        lines.append(f"├─ Withdrawn  -${ws['withdrawn']:,.2f}")
    lines.append(f"├─ Deployed   ${ws['deployed']:,.2f}  ({ws['pending_count']} open positions)")
    avail_sign = "+" if ws["available"] >= 0 else ""
    lines.append(f"└─ Available  {avail_sign}${ws['available']:,.2f}")

    # PnL breakdown
    lines.append(f"\n📊 *Realized PnL*")
    lines.append(f"All-time:  *${ws['realized_pnl']:+,.2f}*")
    if abs(ws["pnl_week"]) > 0:
        lines.append(f"This week: ${ws['pnl_week']:+,.2f}")
    if abs(ws["pnl_today"]) > 0:
        lines.append(f"Today:     ${ws['pnl_today']:+,.2f}")

    # Transaction history (last 5)
    if txns:
        lines.append(f"\n_Last transactions:_")
        for t in txns[-5:][::-1]:
            ts   = t["timestamp"][:10]
            sign = "+" if t["type"] == "deposit" else "-"
            note = f" · {t['note']}" if t.get("note") else ""
            lines.append(f"  {ts}  {sign}${t['amount']:,.2f}  ({t['type']}{note})")

    return "\n".join(lines)


def fmt_positions(uid: int) -> str:
    csv_path = _active_trades_csv_path(uid)
    if not csv_path.exists():
        return "No open positions."

    with csv_path.open() as f:
        rows = list(csv.DictReader(f))

    # Exclude errored live rows (order never placed → not a real position).
    pending = [r for r in rows if not r.get("resolved_at") and not r.get("error")]
    if not pending:
        return "✅ No open positions — all trades resolved."

    total_deployed = sum(float(r["size_usd"]) for r in pending if r.get("size_usd"))

    # Group by resolution date
    by_date: dict[str, list] = defaultdict(list)
    for r in pending:
        d = (r.get("resolution_date") or "?")[:10]
        by_date[d].append(r)

    lines = [
        f"*📍 Open Positions* — {len(pending)} trades · ${total_deployed:,.0f} deployed\n"
    ]

    for date in sorted(by_date.keys()):
        trades   = by_date[date]
        at_risk  = sum(float(t["size_usd"]) for t in trades if t.get("size_usd"))
        avg_edge = sum(float(t.get("edge_pp", 0)) for t in trades) / len(trades)

        # Group by city within this date
        city_groups: dict[str, list] = defaultdict(list)
        for t in trades:
            city_groups[_extract_city(t.get("market_title", ""))].append(t)

        city_str = "  ".join(
            f"{city}×{len(ts)}" for city, ts in sorted(city_groups.items())
        )

        lines.append(f"📅 *{date}*  —  {len(trades)} trades · ${at_risk:,.0f} at risk · avg {avg_edge:.0%} edge")
        lines.append(f"   {city_str}")

        # Show individual trades (up to 6 per date to stay readable)
        shown = trades[:6]
        for t in shown:
            d_icon  = "📈" if t.get("direction") == "YES" else "📉"
            size    = float(t.get("size_usd", 0))
            edge    = float(t.get("edge_pp", 0))
            city    = _extract_city(t.get("market_title", ""))
            # Strip city prefix from title to save space
            title   = t.get("market_title", "?")
            # Take the part after " - " (the specific question)
            if " - " in title:
                title = title.split(" - ", 1)[1]
            title = title[:52]
            lines.append(f"   {d_icon} ${size:.0f} · {edge:.0%} · _{title}_")
        if len(trades) > 6:
            lines.append(f"   _...and {len(trades) - 6} more_")

        lines.append("")  # spacer between dates

    return "\n".join(lines).rstrip()


def fmt_signals(signals: list[dict], uid: int) -> str:
    if not signals:
        return "No signals from last scan."
    scanned_at = ""
    f = _signals_path(uid)
    if f.exists():
        scanned_at = json.loads(f.read_text()).get("scanned_at", "")[:16].replace("T", " ")
    lines = [f"*🎯 Last Scan Signals* — {len(signals)} total"]
    if scanned_at:
        lines.append(f"_Scanned: {scanned_at} UTC_\n")
    for s in signals[:10]:
        d       = "📈 YES" if s["direction"] == "YES" else "📉 NO"
        edge    = s.get("edge_pp", 0)
        model_p = s.get("model_p", 0)
        mkt_p   = s.get("mkt_p", 0)
        title   = s.get("title", "?")[:50]
        lines.append(f"{d} *{edge:.0%}* edge — _{title}_")
        lines.append(f"   Model {model_p:.1%} vs Mkt {mkt_p:.1%}")
    if len(signals) > 10:
        lines.append(f"\n_...and {len(signals) - 10} more_")
    return "\n".join(lines)


def fmt_trades(uid: int, n: int = 10) -> str:
    trades_csv = _active_trades_csv_path(uid)
    if not trades_csv.exists():
        return "No trades yet."
    with trades_csv.open() as f:
        rows = list(csv.DictReader(f))
    resolved = sorted(
        [r for r in rows if r.get("resolved_at")],
        key=lambda x: x.get("resolved_at", ""), reverse=True
    )[:n]
    if not resolved:
        return "No resolved trades yet."
    scope = "🟢 Live" if get_user_mode(uid) == "live" else "🟡 Paper"
    lines = [f"*📋 Last {len(resolved)} Resolved Trades* — {scope}\n"]
    for r in resolved:
        pnl       = float(r.get("pnl_usd", 0))
        e         = "✅" if pnl > 0 else "❌"
        title     = r.get("market_title", "?")
        if " - " in title:
            title = title.split(" - ", 1)[1]
        title     = title[:48]
        direction = r.get("direction", "?")
        edge      = float(r.get("edge_pp", 0))
        brier     = r.get("brier_score", "")
        brier_str = f" · brier {float(brier):.3f}" if brier else ""
        lines.append(f"{e} *${pnl:+.2f}* · {direction} · {edge:.0%} edge{brier_str}")
        lines.append(f"   _{title}_  `/why {r.get('trade_id', '')}`")
    lines.append("\n_Tap a ❓ button below for the full reasoning._")
    return "\n".join(lines)


def why_kb(uid: int, n: int = 10) -> InlineKeyboardMarkup | None:
    """❓ buttons for the trades shown by fmt_trades (same order, max 8)."""
    trades_csv = _active_trades_csv_path(uid)
    if not trades_csv.exists():
        return None
    with trades_csv.open() as f:
        rows = list(csv.DictReader(f))
    resolved = sorted(
        [r for r in rows if r.get("resolved_at")],
        key=lambda x: x.get("resolved_at", ""), reverse=True
    )[:min(n, 8)]
    if not resolved:
        return None
    buttons = [
        InlineKeyboardButton(f"❓ {i + 1}", callback_data=f"why:{r.get('trade_id', '')}")
        for i, r in enumerate(resolved)
    ]
    rows_kb = [buttons[i:i + 4] for i in range(0, len(buttons), 4)]
    rows_kb.append([InlineKeyboardButton("🔙 Back", callback_data="status")])
    return InlineKeyboardMarkup(rows_kb)


# ── Keyboards ─────────────────────────────────────────────────────────────────

def main_kb(uid: int) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("📊 Status",    callback_data="status"),
            InlineKeyboardButton("💼 Wallet",    callback_data="wallet"),
        ],
        [
            InlineKeyboardButton("📍 Positions", callback_data="positions"),
            InlineKeyboardButton("🎯 Signals",   callback_data="signals"),
        ],
        [InlineKeyboardButton("📋 Trades",       callback_data="trades")],
    ]
    if has_permission(uid, perms.TRIGGER_SCAN):
        rows.append([
            InlineKeyboardButton("🔍 Scan",    callback_data="scan"),
            InlineKeyboardButton("✅ Resolve", callback_data="resolve"),
        ])
    if has_permission(uid, perms.MANAGE_USERS):
        rows.append([InlineKeyboardButton("👥 Users", callback_data="users")])
    return InlineKeyboardMarkup(rows)

def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="status")]])

def confirm_kb(action: str, label: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(f"✅ {label}", callback_data=f"{action}_confirm"),
        InlineKeyboardButton("❌ Cancel",   callback_data="status"),
    ]])


# ── Helpers ───────────────────────────────────────────────────────────────────

# One scan serves all users (one model, fan-out mirrors results). The lock keeps
# concurrent /scan presses from spawning parallel subprocesses that would hammer
# the forecast APIs and race on the CSVs.
_bot_run_lock = asyncio.Lock()

# Per-user scan cooldown: prevents a non-admin from re-triggering a scan the
# moment the lock releases.  Admins are exempt (they run scheduled scans anyway).
import time as _time
_scan_last: dict[int, float] = {}
SCAN_COOLDOWN_SECS = 60

def _scan_cooldown_remaining(uid: int) -> float:
    elapsed = _time.monotonic() - _scan_last.get(uid, 0.0)
    return max(0.0, SCAN_COOLDOWN_SECS - elapsed)

async def run_bot_async(mode: str, uid: int) -> tuple[str, str, int]:
    """Run weather_bot.py globally: root scan/resolve + fan-out to all users."""
    if _bot_run_lock.locked():
        return "", "A scan or resolve is already running — try again in a minute.", -2
    async with _bot_run_lock:
        args = [PYTHON, "weather_bot.py", "--mode", mode, "--all-users"]
        if mode == "paper":
            args += ["--interval", "0"]
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=ROOT,
            env=os.environ.copy(),
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
        except asyncio.TimeoutError:
            proc.kill()
            return "", "Timed out after 300s.", -1
        return stdout.decode(), stderr.decode(), proc.returncode or 0


# ── Command handlers ───────────────────────────────────────────────────────────

@require_auth()
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lines = [
        "*Portfolio*",
        "/status — overview: return %, win rate, gates",
        "/paperstats — paper/model record (always available, even when live)",
        "/wallet — balance, deployed, PnL breakdown",
        "/positions — open trades grouped by date & city",
        "/trades [n] — last N resolved trades (default 10)",
        "/signals — signals from the last scan\n",
        "*Transparency*",
        "/why <trade_id> — full reasoning behind a trade",
        "/scanreport — scan funnel: what was fetched, rejected, taken",
        "/losses [n] — losing trades with causes\n",
        "*Wallet & withdrawals*",
        "/deposit — paper: record simulated capital · live: fund address + balance",
        "/exportkey — reveal your key (60s) to view/trade on Polymarket",
        "/withdraw <amount|all> <0xaddr> — withdraw to an allowlisted address",
        "/allowlist — view allowlist & daily cap",
        "/allowlist\\_add <0xaddr> [label] — allowlist an address (24h cooling-off)",
        "/allowlist\\_remove <0xaddr> — remove an address\n",
        "*Bot control*",
        "/scan — trigger a market scan",
        "/resolve — auto-resolve pending paper trades",
        "/mymode [paper|live] — view or change mode",
        "/wallet\\_setup — create or connect your trading wallet",
    ]
    if is_admin(uid):
        lines += [
            "\n*Admin*",
            "/invite [admin|trader|viewer] — create an invite link",
            "/adduser <id> [admin|trader|viewer] — add user",
            "/removeuser <id> — remove user",
            "/setrole <id> <role> — change a user's role",
            "/suspend <id> · /unsuspend <id> — block / restore access",
            "/users — list users",
            "/audit [n] — recent audit log",
            "/setwithdrawcap <id> <usd> — set a user's daily withdrawal cap",
            "/setmaxbet <usd> — set global max bet per trade",
            "/setup <key> [proxy] — save credentials (legacy)",
        ]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

@require_auth()
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.effective_message.reply_text(
        fmt_status(uid), reply_markup=main_kb(uid), parse_mode="Markdown",
    )

@require_auth()
async def cmd_paperstats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """The paper/model track record — always available, even when trading live
    (it keeps feeding calibration)."""
    uid = update.effective_user.id
    await update.effective_message.reply_text(
        _fmt_status_paper(uid), reply_markup=main_kb(uid), parse_mode="Markdown",
    )

async def _autodelete_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await ctx.bot.delete_message(ctx.job.data["chat_id"], ctx.job.data["message_id"])
    except Exception:
        pass

@require_auth()
async def cmd_exportkey(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Reveal YOUR OWN private key once (auto-deletes in 60s) so you can import it
    into a browser wallet and view/trade this account on polymarket.com. Audited."""
    uid = update.effective_user.id
    from weather.secrets import get_user_creds
    creds = get_user_creds(uid) or {}
    pk = creds.get("pk")
    if not pk:
        await update.effective_message.reply_text(
            "No key stored — run /wallet\\_setup first.", parse_mode="Markdown")
        return
    audit_log("private_key_revealed", actor=uid)
    wallet = creds.get("funder_address") or ""
    view = (f"\n\n👁 View read-only (no key needed):\n"
            f"polymarket.com/profile/{wallet}\npolygonscan.com/address/{wallet}") if wallet else ""
    msg = await update.effective_message.reply_text(
        "🔑 *Your private key* (auto-deletes in 60s — copy it NOW):\n"
        f"`{pk}`\n\n"
        "Import it into a browser wallet (e.g. MetaMask) → *Connect Wallet* on "
        "polymarket.com to view/trade this account.\n"
        "⚠️ Anyone with this key controls your funds — only do this on a device you "
        "trust, and never share it." + view,
        parse_mode="Markdown",
    )
    ctx.job_queue.run_once(_autodelete_job, 60,
                           data={"chat_id": msg.chat_id, "message_id": msg.message_id})

@require_auth()
async def cmd_wallet(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.effective_message.reply_text(
        fmt_wallet(uid), reply_markup=main_kb(uid), parse_mode="Markdown",
    )

@require_auth()
async def cmd_positions(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.effective_message.reply_text(
        fmt_positions(uid), reply_markup=main_kb(uid), parse_mode="Markdown",
    )

@require_auth()
async def cmd_signals(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.effective_message.reply_text(
        fmt_signals(read_last_signals(uid), uid),
        reply_markup=main_kb(uid),
        parse_mode="Markdown",
    )

@require_auth()
async def cmd_trades(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    n   = 10
    if ctx.args:
        try:
            n = min(int(ctx.args[0]), 30)
        except ValueError:
            pass
    await update.effective_message.reply_text(
        fmt_trades(uid, n), reply_markup=why_kb(uid, n) or main_kb(uid), parse_mode="Markdown",
    )

@require_auth()
async def cmd_why(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Explain one trade. Usage: /why <trade_id>"""
    uid = update.effective_user.id
    if not ctx.args:
        await update.message.reply_text(
            "Usage: `/why <trade_id>`\nTrade IDs are shown in /trades and /losses.",
            parse_mode="Markdown",
        )
        return
    trades_csv = _active_trades_csv_path(uid)
    rows: list[dict] = []
    if trades_csv.exists():
        with trades_csv.open() as f:
            rows = list(csv.DictReader(f))
    row = telegram_views.find_trade(rows, ctx.args[0])
    if row is None:
        await update.message.reply_text("❌ Trade not found (or ambiguous prefix).")
        return
    await update.message.reply_text(
        telegram_views.fmt_why(row), reply_markup=back_kb(), parse_mode="Markdown",
    )

@require_auth()
async def cmd_scanreport(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show the scan funnel: fetched → parsed → gates → actionable."""
    uid = update.effective_user.id
    f = _signals_path(uid)
    data = {}
    if f.exists():
        try:
            data = json.loads(f.read_text())
        except json.JSONDecodeError:
            pass
    await update.effective_message.reply_text(
        telegram_views.fmt_scanreport(data), reply_markup=main_kb(uid), parse_mode="Markdown",
    )

@require_auth()
async def cmd_losses(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Resolved losing trades with causes. Usage: /losses [n]"""
    uid = update.effective_user.id
    n   = 10
    if ctx.args:
        try:
            n = min(int(ctx.args[0]), 30)
        except ValueError:
            pass
    trades_csv = _active_trades_csv_path(uid)
    rows: list[dict] = []
    if trades_csv.exists():
        with trades_csv.open() as f:
            rows = list(csv.DictReader(f))
    await update.effective_message.reply_text(
        telegram_views.fmt_losses(rows, n), reply_markup=main_kb(uid), parse_mode="Markdown",
    )

def _deposit_refresh_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Check balance", callback_data="deposit_refresh")]])


async def _live_deposit_reply(target, uid: int) -> None:
    """Live-mode /deposit: show the on-chain deposit wallet + live balance, and
    record any newly-arrived USDC.e as a real deposit. The bot cannot pull funds —
    the user pushes USDC.e (Polygon) to this address themselves."""
    from weather.secrets import get_user_creds
    creds  = get_user_creds(uid) or {}
    wallet = creds.get("funder_address")
    if not wallet or creds.get("signature_type") != 3:
        await target.reply_text(
            "⚠️ No deposit wallet configured yet. Run /wallet\\_setup first, then "
            "come back to fund it.",
            parse_mode="Markdown",
        )
        return
    try:
        from weather import relayer, live_ledger
        # Two independent JSON-RPC calls — overlap them instead of blocking serially.
        usdce, pusd = await asyncio.wait_for(
            asyncio.gather(
                asyncio.to_thread(relayer.usdce_balance, wallet),
                asyncio.to_thread(relayer.pusd_balance, wallet),
            ),
            timeout=30,
        )
    except Exception as exc:  # noqa: BLE001
        await target.reply_text(
            f"⚠️ Couldn't read the on-chain balance ({type(exc).__name__}). "
            "Funds may still be in transit — try again shortly.",
        )
        return
    detected = live_ledger.reconcile_deposit(_live_wallet_file(uid), usdce)
    net = live_ledger.net_deposited(_live_wallet_file(uid))
    got = (f"\n✅ *New deposit detected: +${detected:,.2f}* USDC.e — it wraps to "
           f"tradeable pUSD automatically at go-live." if detected > 0 else "")
    await target.reply_text(
        "🟢 *LIVE — fund your wallet*\n\n"
        "Send *USDC.e on the Polygon network* to:\n"
        f"`{wallet}`\n\n"
        "• Polygon network, *USDC.e* (bridged) — not native USDC\n"
        "• Include a little *POL* (~0.2) for gas\n\n"
        f"On-chain now:\n• USDC.e (just deposited): *${usdce:,.2f}*\n"
        f"• pUSD (tradeable): *${pusd:,.2f}*\n"
        f"Total real deposits recorded: *${net:,.2f}*"
        + got,
        reply_markup=_deposit_refresh_kb(),
        parse_mode="Markdown",
    )


@require_perm(perms.DEPOSIT_OWN)
async def cmd_deposit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Fund your wallet. Paper mode records simulated capital; live mode shows the
    on-chain deposit address + balance. Usage (paper): /deposit <amount> [note]"""
    uid = update.effective_user.id
    if get_user_mode(uid) == "live":
        await _live_deposit_reply(update.message, uid)
        return
    # Paper mode — simulated capital for tracking paper ROI. No real money moves.
    if not ctx.args:
        await update.message.reply_text(
            "📝 *Paper mode* — `/deposit <amount>` records *simulated* capital to "
            "track paper ROI. No real money moves.\n"
            "Example: `/deposit 500 initial load`\n\n"
            "_Switch to live (`/mymode live`) to fund a real wallet._",
            parse_mode="Markdown",
        )
        return
    try:
        amount = float(ctx.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid amount.")
        return
    if amount <= 0 or not math.isfinite(amount):
        await update.message.reply_text("❌ Amount must be a positive finite number.")
        return
    note = " ".join(ctx.args[1:]) if len(ctx.args) > 1 else ""
    append_wallet_transaction(uid, "deposit", amount, note)
    ws = wallet_stats(uid)
    await update.message.reply_text(
        f"📝 *PAPER deposit recorded:* +${amount:,.2f} _(simulated — no real money)_\n"
        f"Total simulated: ${ws['deposited']:,.2f}\n"
        f"Paper balance:   ${ws['wallet_balance']:,.2f}",
        parse_mode="Markdown",
    )

@require_perm(perms.WITHDRAW_OWN)
async def cmd_withdraw(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Withdraw real pUSD off-chain as USDC.e to an external address (gasless via
    the relayer). Usage: /withdraw <amount|all> <0xADDRESS>"""
    import asyncio
    from weather.secrets import get_user_creds
    from weather import relayer

    uid = update.effective_user.id
    if len(ctx.args) < 2:
        await update.message.reply_text(
            "Usage: `/withdraw <amount|all> <0xADDRESS>`\n"
            "Example: `/withdraw 5 0xYourWallet…`  ·  `/withdraw all 0x…`\n\n"
            "The destination must be *allowlisted* (`/allowlist_add`) and past its "
            "cooling-off window. Sends real USDC.e from your deposit wallet.",
            parse_mode="Markdown",
        )
        return

    if withdraw_rate_limited(uid):
        await update.message.reply_text(
            "⏳ Too many withdrawal attempts recently — try again later."
        )
        return

    creds = get_user_creds(uid) or {}
    wallet = creds.get("funder_address")
    if not wallet or not creds.get("pk") or creds.get("signature_type") != 3:
        await update.message.reply_text(
            "❌ No deposit wallet configured for on-chain withdrawal.\n"
            "Set one up first (admin: `enable_deposit_wallet`).",
            parse_mode="Markdown",
        )
        return

    to = ctx.args[1]
    if wpol.normalize_address(to) is None:
        await update.message.reply_text("❌ Invalid destination address (need 0x + 40 hex).")
        return

    balance = await asyncio.to_thread(relayer.pusd_balance, wallet)
    amt_str = ctx.args[0].lower()
    if amt_str == "all":
        amount = balance
    else:
        try:
            amount = float(amt_str)
        except ValueError:
            await update.message.reply_text("❌ Invalid amount.")
            return
    if amount <= 0 or not math.isfinite(amount):
        await update.message.reply_text("❌ Amount must be a positive finite number.")
        return
    if amount > balance + 1e-9:
        await update.message.reply_text(
            f"❌ Amount ${amount:,.2f} exceeds deposit-wallet balance ${balance:,.2f}."
        )
        return

    # ── Phase C policy: allowlist + cooling-off + daily cap + large-withdrawal code ──
    code_arg = ctx.args[2] if len(ctx.args) > 2 else None
    staged = ctx.user_data.get("withdraw_code")
    code_ok = bool(
        staged and code_arg
        and code_arg == staged.get("code")
        and abs(float(staged.get("amount", 0)) - amount) < 1e-9
        and wpol.normalize_address(staged.get("to", "")) == wpol.normalize_address(to)
    )
    decision = wpol.evaluate_withdrawal(
        amount, to, get_withdraw_allowlist(uid),
        withdrawn_today(uid), get_withdraw_cap(uid), code_ok=code_ok,
    )
    if not decision.allowed:
        if decision.requires_code:
            code = f"{int.from_bytes(os.urandom(3), 'big') % 1_000_000:06d}"
            ctx.user_data["withdraw_code"] = {"code": code, "amount": amount, "to": to}
            audit_log("withdrawal_large_requested", actor=uid, amount=amount, to=to)
            if uid != ADMIN_ID:  # owner oversight / audit trail on large withdrawals
                await notify_owner(
                    ctx.bot,
                    f"🔔 Large withdrawal requested\nuser `{uid}` · ${amount:,.2f} → `{to}`",
                )
            await update.message.reply_text(
                f"🔐 *Large withdrawal* (≥ ${WITHDRAW_LARGE_USD:,.0f}) needs a code.\n"
                f"Confirmation code: `{code}`\n\n"
                f"Re-run to proceed:\n`/withdraw {amt_str} {to} {code}`",
                parse_mode="Markdown",
            )
            return
        await update.message.reply_text(f"🚫 {decision.reason}")
        return

    ctx.user_data.pop("withdraw_code", None)
    token = os.urandom(6).hex()
    ctx.user_data["withdraw_pending"] = {
        "token": token, "amount": amount,
        # Floor, never round up — a raw amount above the on-chain balance reverts
        # the whole gasless unwrap batch (matters most for the "all" path).
        "amount_raw": int(amount * 1_000_000), "to": to,
    }
    await update.message.reply_text(
        f"⚠️ *Confirm withdrawal*\n\n"
        f"Send *${amount:,.2f}* (USDC.e) from your deposit wallet to:\n"
        f"`{to}`\n\n"
        f"Deposit-wallet balance: ${balance:,.2f}\nThis moves real funds.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Confirm withdrawal", callback_data=f"withdrawconfirm:{token}"),
            InlineKeyboardButton("❌ Cancel", callback_data="status"),
        ]]),
        parse_mode="Markdown",
    )

@require_perm(perms.WITHDRAW_OWN)
async def cmd_allowlist(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show the user's withdrawal allowlist + daily-cap status."""
    uid = update.effective_user.id
    entries = get_withdraw_allowlist(uid)
    now = datetime.now(timezone.utc)
    cap = get_withdraw_cap(uid)
    spent = withdrawn_today(uid)
    lines = [
        "*💸 Withdrawal allowlist*",
        f"Daily cap ${cap:,.0f} · today ${spent:,.2f} · left ${max(0.0, cap - spent):,.2f}\n",
    ]
    if not entries:
        lines.append("_No addresses yet._ Add one with `/allowlist_add 0x…`")
    else:
        for e in entries:
            state = wpol.allowlist_state(entries, e["address"], now)
            icon = {"active": "✅", "cooling": "⏳"}.get(state, "❓")
            lbl = f" — {e['label']}" if e.get("label") else ""
            extra = ""
            if state == "cooling":
                added = wpol._parse_ts(e.get("added_at", ""))
                if added:
                    ready = added + timedelta(hours=WITHDRAW_COOLING_OFF_HOURS)
                    hrs = max(0.0, (ready - now).total_seconds() / 3600)
                    extra = f" (ready in {hrs:.0f}h)"
            lines.append(f"{icon} `{e['address']}`{lbl}{extra}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@require_perm(perms.WITHDRAW_OWN)
async def cmd_allowlist_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not ctx.args:
        await update.message.reply_text("Usage: `/allowlist_add <0xADDRESS> [label]`", parse_mode="Markdown")
        return
    addr = wpol.normalize_address(ctx.args[0])
    if not addr:
        await update.message.reply_text("❌ Invalid address (need 0x + 40 hex).")
        return
    label = " ".join(ctx.args[1:])[:40]
    add_allowlist_entry(uid, addr, label)
    audit_log("allowlist_add", actor=uid, address=addr, label=label or None)
    await update.message.reply_text(
        f"✅ Allowlisted `{addr}`.\n"
        f"⏳ Usable for withdrawals in {WITHDRAW_COOLING_OFF_HOURS:.0f}h "
        "(cooling-off protects against a compromised session).",
        parse_mode="Markdown",
    )


@require_perm(perms.WITHDRAW_OWN)
async def cmd_allowlist_remove(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not ctx.args:
        await update.message.reply_text("Usage: `/allowlist_remove <0xADDRESS>`", parse_mode="Markdown")
        return
    if remove_allowlist_entry(uid, ctx.args[0]):
        audit_log("allowlist_remove", actor=uid, address=wpol.normalize_address(ctx.args[0]))
        await update.message.reply_text("✅ Removed from your allowlist.")
    else:
        await update.message.reply_text("Address not found in your allowlist.")


@require_perm(perms.MANAGE_USERS)
async def cmd_setwithdrawcap(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) < 2:
        await update.message.reply_text("Usage: `/setwithdrawcap <telegram_id> <usd>`", parse_mode="Markdown")
        return
    try:
        target = int(ctx.args[0])
        usd = float(ctx.args[1])
    except ValueError:
        await update.message.reply_text("❌ Need `<telegram_id> <usd>`.", parse_mode="Markdown")
        return
    if not (0 <= usd <= 100_000):
        await update.message.reply_text("❌ Cap must be between $0 and $100,000.")
        return
    set_withdraw_cap(target, usd)
    audit_log("withdraw_cap_set", actor=update.effective_user.id, target=target, cap=usd)
    await update.message.reply_text(
        f"✅ Daily withdrawal cap for `{target}` set to *${usd:,.0f}*.", parse_mode="Markdown"
    )


@require_perm(perms.SET_MAXBET)
async def cmd_setmaxbet(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from weather.config import MAX_LIVE_TRADE_USD
    from weather.live_trader import _RUNTIME_CONFIG
    runtime_cfg = DATA_DIR / "logs" / "runtime_config.json"

    if not ctx.args:
        current = MAX_LIVE_TRADE_USD
        try:
            if runtime_cfg.exists():
                cfg = json.loads(runtime_cfg.read_text())
                current = float(cfg.get("max_trade_usd", current))
        except Exception:
            pass
        await update.effective_message.reply_text(
            f"💰 Current max bet: *${current:.0f}/trade*\n\nChange with: /setmaxbet 50",
            parse_mode="Markdown",
        )
        return

    try:
        val = float(ctx.args[0])
        if not (1 <= val <= 500):
            await update.effective_message.reply_text("❌ Must be between $1 and $500.")
            return
    except ValueError:
        await update.effective_message.reply_text("❌ Enter a number, e.g. /setmaxbet 50")
        return

    try:
        cfg = json.loads(runtime_cfg.read_text()) if runtime_cfg.exists() else {}
    except Exception:
        cfg = {}
    old = cfg.get("max_trade_usd", MAX_LIVE_TRADE_USD)
    cfg["max_trade_usd"] = val
    runtime_cfg.parent.mkdir(parents=True, exist_ok=True)
    runtime_cfg.write_text(json.dumps(cfg, indent=2))
    await update.effective_message.reply_text(
        f"✅ Max bet: *${old:.0f} → ${val:.0f}/trade*\nActive on next trade — no restart needed.",
        parse_mode="Markdown",
    )


@require_perm(perms.TRIGGER_SCAN)
async def cmd_scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not has_permission(uid, perms.BYPASS_SCAN_COOLDOWN):
        wait = _scan_cooldown_remaining(uid)
        if wait > 0:
            await update.effective_message.reply_text(
                f"⏳ Please wait {wait:.0f}s before scanning again."
            )
            return
    _scan_last[uid] = _time.monotonic()
    msg = await update.effective_message.reply_text("🔍 Scanning Polymarket...")
    stdout, stderr, rc = await run_bot_async("paper", uid)
    if rc == -2:
        await msg.edit_text(f"⏳ {stderr}", reply_markup=main_kb(uid))
    elif rc != 0:
        err = (stderr or stdout or "Unknown error")[-300:].strip()
        await msg.edit_text(
            f"❌ Scan failed\n```\n{err}\n```", reply_markup=main_kb(uid), parse_mode="Markdown"
        )
    else:
        summary = next((l for l in stdout.splitlines() if "evaluated" in l), "Scan complete.")
        await msg.edit_text(f"✅ {summary.strip()}", reply_markup=main_kb(uid))

@require_perm(perms.TRIGGER_SCAN)
async def cmd_resolve(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    msg = await update.effective_message.reply_text("⏳ Running auto-resolve...")
    stdout, stderr, rc = await run_bot_async("auto-resolve", uid)
    if rc == -2:
        await msg.edit_text(f"⏳ {stderr}", reply_markup=main_kb(uid))
    elif rc != 0:
        err = (stderr or stdout or "Unknown error")[-300:].strip()
        await msg.edit_text(
            f"❌ Resolve failed\n```\n{err}\n```", reply_markup=main_kb(uid), parse_mode="Markdown"
        )
    else:
        summary = next((l for l in stdout.splitlines() if "Auto-resolved" in l), "Done.")
        await msg.edit_text(f"✅ {summary.strip()}", reply_markup=main_kb(uid))

@require_perm(perms.USE_LEGACY_SETUP)
async def cmd_setup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    # Strip an explicit `replace` / `confirm` token from anywhere in the args.
    replace = any(a.lower() in ("replace", "confirm") for a in ctx.args)
    args = [a for a in ctx.args if a.lower() not in ("replace", "confirm")]
    if not args:
        await update.message.reply_text(
            "*Setup your Polymarket account*\n\n"
            "Usage: `/setup <private_key> [proxy_address]`\n\n"
            "Your private key is stored *encrypted* in `user_keys.enc.json` "
            "(never in plain text). Send this command in a private chat with the bot.",
            parse_mode="Markdown",
        )
        return
    # Guard against silently replacing an existing wallet (fund-loss risk).
    if get_user_key(uid) and not replace:
        await update.message.reply_text(
            "⚠️ *You already have a wallet.* `/setup` would replace its key — the old "
            "key (and any funds) become *unrecoverable* unless backed up.\n\n"
            "Back up first (`/wallet_setup` → 🔑 reveal), then run "
            "`/setup <private_key> replace` to proceed.",
            parse_mode="Markdown",
        )
        return
    # Store key encrypted; scrub the plaintext arg from memory immediately
    raw_key = args[0]
    for i in range(len(ctx.args)):
        ctx.args[i] = "***"
    try:
        set_user_key(uid, raw_key)
    except RuntimeError as exc:
        await update.message.reply_text(
            f"❌ Key storage unavailable: {exc}\n"
            "Set `POLYMARKET_SECRETS_KEY` in `.env` or install `keyring`.",
            parse_mode="Markdown",
        )
        return

    if len(args) > 1:
        proxy = args[1]
        set_user_creds(uid, proxy_address=proxy, signature_type="gnosis-safe")
        with _users_transaction() as users:
            users[uid]["proxy_address"] = proxy
            users[uid]["signature_type"] = "gnosis-safe"

    # Best-effort: delete the user's message to remove the key from chat history
    try:
        await update.message.delete()
        key_deleted = True
    except Exception:
        key_deleted = False

    # Derive L2 CLOB credentials from L1 key (non-blocking, best-effort)
    try:
        import asyncio as _asyncio
        await _asyncio.wait_for(
            _asyncio.to_thread(derive_and_store_clob_creds, uid),
            timeout=20,
        )
        l2_note = "🔑 L2 trading credentials derived and stored.\n"
    except Exception:
        l2_note = "⚠️ L2 credential derivation failed — balance checks may show $0. Run /setup again to retry.\n"

    reply = (
        f"✅ Credentials saved (encrypted).\n{l2_note}\n"
        "Use `/mymode live` to switch to live trading.\n\n"
    )
    if not key_deleted:
        reply += (
            "⚠️ *Please delete your message containing the private key* — "
            "it remains visible in this chat and may be stored by Telegram."
        )
    await update.message.reply_text(reply, parse_mode="Markdown")

def _log_startup() -> None:
    from weather import secrets as _sec
    if _sec._get_keyring() is not None:
        backend = "OS keychain"
    elif _sec._get_fernet() is not None:
        backend = "Fernet (AES-128, key from POLYMARKET_SECRETS_KEY)"
    else:
        backend = "NONE — key storage unavailable, /setup will fail"
    print(f"[startup] key backend : {backend}", flush=True)
    print(f"[startup] DATA_DIR    : {DATA_DIR}", flush=True)
    print(f"[startup] PYTHON_BIN  : {PYTHON}", flush=True)


def _security_self_check() -> list[str]:
    """Advisory boot-time checks (audit F3). Returns human-readable issue strings."""
    from weather.secrets import get_user_creds, _ENC_KEYS_FILE
    issues: list[str] = []

    # 1. Sensitive files must not be group/other-accessible.
    for label, p in (
        (".env", ROOT / ".env"),
        ("key store", _ENC_KEYS_FILE),
        ("users.json", USERS_FILE),
    ):
        try:
            mode = p.stat().st_mode & 0o777
            if mode & 0o077:
                # Auto-remediate: tighten to 0600 so a drift (deploy, manual edit)
                # can't leave a sensitive file readable. Still report it so the
                # owner knows a drift happened and was corrected.
                try:
                    os.chmod(p, 0o600)
                    issues.append(f"{label} was group/other-accessible ({oct(mode)}) — auto-fixed to 0600")
                except OSError:
                    issues.append(f"{label} is group/other-accessible ({oct(mode)}) — auto-fix failed")
        except OSError:
            pass

    # 2. Master key should come from a systemd credential, not the env var.
    if os.environ.get("POLYMARKET_SECRETS_KEY") and not os.environ.get("CREDENTIALS_DIRECTORY"):
        issues.append("master key is in POLYMARKET_SECRETS_KEY env (prefer systemd LoadCredential)")

    # 3. The owner should have stored credentials.
    if not (get_user_creds(ADMIN_ID) or {}).get("pk"):
        issues.append(f"owner {ADMIN_ID} has no stored key")

    return issues


async def _run_self_check(app) -> None:
    try:
        issues = _security_self_check()
    except Exception:
        return
    audit_log("startup_selfcheck", issues=len(issues))
    if issues:
        body = "\n".join(f"  • {i}" for i in issues)
        await notify_owner(app.bot, f"🛡 *Security self-check* found {len(issues)} issue(s):\n{body}")


async def _on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Alert the owner on a getUpdates Conflict (duplicate poller) — throttled."""
    from telegram.error import Conflict
    if isinstance(ctx.error, Conflict):
        if not _alert_throttled(("conflict",)):
            await notify_owner(
                ctx.bot,
                "⚠️ Telegram *getUpdates Conflict* — another bot instance may be "
                "polling the same token. Only one instance should run.",
            )


def _global_gate_check() -> tuple[bool, list[str]]:
    """One model, one track record: live unlocks on the ROOT paper log's gates."""
    from weather.paper_trader import PaperTrader as _PT
    stats = _PT(log_path=DATA_DIR / "logs" / "paper_trades.csv").compute_stats()
    return stats.ready_for_live, stats.failure_reasons

@require_auth()
async def cmd_mymode(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not ctx.args:
        mode = get_user_mode(uid)
        await update.message.reply_text(f"Your current mode: `{mode}`", parse_mode="Markdown")
        return
    new_mode = ctx.args[0].lower()
    if new_mode not in ("paper", "live"):
        await update.message.reply_text("Usage: `/mymode paper` or `/mymode live`", parse_mode="Markdown")
        return

    if new_mode == "paper":
        with _users_transaction() as users:
            users[uid]["mode"] = "paper"
        await update.message.reply_text("✅ Mode set to 🟡 PAPER", parse_mode="Markdown")
        return

    # live: capability + global model gate + own credentials + explicit risk confirmation
    if not has_permission(uid, perms.GO_LIVE):
        await update.message.reply_text(
            "🚫 Your role can't switch to live trading. Ask an admin for `trader` access.",
            parse_mode="Markdown",
        )
        return
    if not (get_user_key(uid) or get_user_private_key(uid)):
        await update.message.reply_text(
            "❌ No wallet connected yet — run /wallet\\_setup first.", parse_mode="Markdown"
        )
        return
    ready, reasons = _global_gate_check()
    if not ready:
        await update.message.reply_text(
            "🔴 The shared model's go-live gates aren't passed:\n"
            + "\n".join(f"  · {r}" for r in reasons),
        )
        return
    # One-time token ties this confirm button to this prompt, so a stale button
    # tapped later (or after a restart) can't silently flip the user to live.
    token = os.urandom(6).hex()
    ctx.user_data["liveconfirm_token"] = token
    await update.message.reply_text(
        "⚠️ *Switching to LIVE trading*\n\n"
        "The bot will place real orders with *your* USDC on every scheduled scan, "
        "sized by Kelly fraction against your balance. Losses are real.\n\n"
        "Confirm?",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Yes, trade my real money", callback_data=f"liveconfirm:{token}"),
            InlineKeyboardButton("❌ Cancel", callback_data="status"),
        ]]),
        parse_mode="Markdown",
    )

@require_perm(perms.VIEW_USERS)
async def cmd_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    users = _load_users()
    lines = ["*👥 Registered Users*\n"]
    for uid, info in users.items():
        lines.append(f"`{uid}` — {info['role']} (@{info.get('username') or '?'})")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@require_perm(perms.MANAGE_USERS)
async def cmd_audit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show recent audit-log entries (money moves, role/user changes, live flips)."""
    n = 20
    if ctx.args:
        try:
            n = min(100, max(1, int(ctx.args[0])))
        except ValueError:
            pass
    recs = read_audit(n)
    if not recs:
        await update.message.reply_text("No audit entries yet.")
        return
    lines = [f"*🧾 Audit — last {len(recs)}*"]
    for r in recs:
        ts = str(r.get("ts", ""))[:19].replace("T", " ")
        act = r.get("action", "?")
        who = r.get("actor", "")
        extra = ""
        if "target" in r:
            extra += f" → {r['target']}"
        if r.get("new_role"):
            extra += f" ({r['new_role']})"
        if r.get("amount") is not None:
            extra += f" ${r['amount']}"
        if r.get("to"):
            extra += f" → {r['to']}"
        lines.append(f"`{ts}` {act} `{who}`{extra}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

@require_perm(perms.MANAGE_USERS)
async def cmd_adduser(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    actor = update.effective_user.id
    grantable = sorted(perms.assignable_roles(get_role(actor)) - {perms.SUSPENDED})
    if not ctx.args:
        await update.message.reply_text(
            f"Usage: /adduser <telegram_id> [{'|'.join(grantable)}]"
        )
        return
    new_id = _parse_target_uid(ctx)
    if new_id is None:
        await update.message.reply_text("❌ Invalid ID — must be a number.")
        return
    role = perms.VIEWER
    if len(ctx.args) > 1:
        role = ctx.args[1].lower()
    if not perms.can_assign_role(get_role(actor), role):
        await update.message.reply_text(
            f"🚫 You can't assign role `{role}`. Allowed: {', '.join(grantable)}.",
            parse_mode="Markdown",
        )
        return
    with _users_transaction() as users:
        users[new_id] = {
            "role": role, "username": "", "added_at": datetime.utcnow().isoformat(),
            "private_key": None, "proxy_address": None, "mode": "paper",
        }
    audit_log("user_added", actor=actor, target=new_id, role=role)
    if role in perms.ADMIN_ROLES:
        await notify_owner(ctx.bot, f"➕ New *{role}* added: `{new_id}` by `{actor}`.")
    await update.message.reply_text(
        f"✅ Added `{new_id}` as `{role}`.", parse_mode="Markdown"
    )

@require_perm(perms.MANAGE_USERS)
async def cmd_removeuser(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: /removeuser <telegram_id>")
        return
    rm_id = _parse_target_uid(ctx)
    if rm_id is None:
        await update.message.reply_text("❌ Invalid ID.")
        return
    if rm_id == ADMIN_ID or get_role(rm_id) == perms.OWNER:
        await update.message.reply_text("❌ Cannot remove the owner.")
        return
    # Only the owner may remove another admin.
    if get_role(rm_id) == perms.ADMIN and get_role(update.effective_user.id) != perms.OWNER:
        await update.message.reply_text("🚫 Only the owner can remove an admin.")
        return
    with _users_lock:
        users = _load_users()
        found = rm_id in users
        if found:
            del users[rm_id]
            _save_users(users)
    if not found:
        await update.message.reply_text("User not found.")
        return
    audit_log("user_removed", actor=update.effective_user.id, target=rm_id)
    await update.message.reply_text(f"✅ Removed `{rm_id}`.", parse_mode="Markdown")


def _parse_target_uid(ctx) -> "int | None":
    try:
        return int(ctx.args[0])
    except (IndexError, ValueError):
        return None


@require_perm(perms.MANAGE_ROLES)
async def cmd_setrole(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    actor = update.effective_user.id
    actor_role = get_role(actor)
    assignable = sorted(perms.assignable_roles(actor_role))
    if len(ctx.args) < 2:
        await update.message.reply_text(
            f"Usage: /setrole <telegram_id> <{'|'.join(assignable)}>"
        )
        return
    target = _parse_target_uid(ctx)
    if target is None:
        await update.message.reply_text("❌ Invalid ID — must be a number.")
        return
    new_role = ctx.args[1].lower()

    if target == actor:
        await update.message.reply_text("🚫 You can't change your own role.")
        return
    if target == ADMIN_ID or get_role(target) == perms.OWNER:
        await update.message.reply_text("❌ The owner's role is fixed.")
        return
    if get_role(target) is None:
        await update.message.reply_text("User not found. Add them first with /adduser.")
        return
    # Touching an existing admin (promote away or demote) is owner-only.
    if get_role(target) == perms.ADMIN and actor_role != perms.OWNER:
        await update.message.reply_text("🚫 Only the owner can change an admin's role.")
        return
    if not perms.can_assign_role(actor_role, new_role):
        await update.message.reply_text(
            f"🚫 You can't assign `{new_role}`. Allowed: {', '.join(assignable)}.",
            parse_mode="Markdown",
        )
        return
    with _users_transaction() as users:
        users.setdefault(target, {})["role"] = new_role
        users[target].pop("role_before_suspend", None)
    audit_log("role_change", actor=actor, target=target, new_role=new_role)
    if new_role in perms.ADMIN_ROLES:
        await notify_owner(ctx.bot, f"🎚 `{target}` set to *{new_role}* by `{actor}`.")
    await update.message.reply_text(
        f"✅ `{target}` is now `{new_role}`.", parse_mode="Markdown"
    )


def _guard_target_manageable(actor: int, target: "int | None") -> "str | None":
    """Shared guard for suspend/unsuspend. Returns an error string, or None if OK."""
    if target is None:
        return "❌ Invalid ID — must be a number."
    if target == actor:
        return "🚫 You can't do that to yourself."
    if target == ADMIN_ID or get_role(target) == perms.OWNER:
        return "❌ The owner cannot be suspended."
    if get_role(target) is None:
        return "User not found."
    if get_role(target) == perms.ADMIN and get_role(actor) != perms.OWNER:
        return "🚫 Only the owner can suspend an admin."
    return None


@require_perm(perms.MANAGE_USERS)
async def cmd_suspend(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    actor = update.effective_user.id
    target = _parse_target_uid(ctx)
    if not ctx.args:
        await update.message.reply_text("Usage: /suspend <telegram_id>")
        return
    err = _guard_target_manageable(actor, target)
    if err:
        await update.message.reply_text(err)
        return
    if get_role(target) == perms.SUSPENDED:
        await update.message.reply_text("Already suspended.")
        return
    with _users_transaction() as users:
        prev = users[target].get("role", perms.VIEWER)
        users[target]["role_before_suspend"] = prev
        users[target]["role"] = perms.SUSPENDED
    audit_log("suspend", actor=actor, target=target, prev_role=prev)
    await update.message.reply_text(
        f"🚫 `{target}` suspended (was `{prev}`). Restore with /unsuspend.",
        parse_mode="Markdown",
    )


@require_perm(perms.MANAGE_USERS)
async def cmd_unsuspend(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    actor = update.effective_user.id
    target = _parse_target_uid(ctx)
    if not ctx.args:
        await update.message.reply_text("Usage: /unsuspend <telegram_id>")
        return
    if target is None:
        await update.message.reply_text("❌ Invalid ID — must be a number.")
        return
    if get_role(target) != perms.SUSPENDED:
        await update.message.reply_text("User is not suspended.")
        return
    with _users_transaction() as users:
        prev = users[target].pop("role_before_suspend", perms.VIEWER)
        # Restoring an elevated role is owner-only; admins restore to viewer.
        if prev in perms.ADMIN_ROLES and get_role(actor) != perms.OWNER:
            prev = perms.VIEWER
        users[target]["role"] = prev
    audit_log("unsuspend", actor=actor, target=target, restored_role=prev)
    await update.message.reply_text(
        f"✅ `{target}` restored to `{prev}`.", parse_mode="Markdown"
    )


# ── Callback handler (inline buttons) ─────────────────────────────────────────

@require_auth()
async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    await q.answer()
    uid  = update.effective_user.id
    data = q.data

    if data == "status":
        await q.edit_message_text(
            fmt_status(uid), reply_markup=main_kb(uid), parse_mode="Markdown"
        )
    elif data == "wallet":
        await q.edit_message_text(
            fmt_wallet(uid), reply_markup=main_kb(uid), parse_mode="Markdown"
        )
    elif data == "positions":
        await q.edit_message_text(
            fmt_positions(uid), reply_markup=main_kb(uid), parse_mode="Markdown"
        )
    elif data == "signals":
        await q.edit_message_text(
            fmt_signals(read_last_signals(uid), uid), reply_markup=main_kb(uid), parse_mode="Markdown"
        )
    elif data == "trades":
        await q.edit_message_text(
            fmt_trades(uid), reply_markup=why_kb(uid) or main_kb(uid), parse_mode="Markdown"
        )
    elif data == "deposit_refresh" and has_permission(uid, perms.DEPOSIT_OWN):
        await _live_deposit_reply(q.message, uid)
    elif data.startswith("liveconfirm"):
        if not has_permission(uid, perms.GO_LIVE):
            await q.edit_message_text("🚫 Your role can't switch to live trading.", reply_markup=main_kb(uid))
            return
        # One-time token: rejects a stale confirm button from an earlier prompt
        # (or one left over across a bot restart) so it can't flip mode to live.
        token    = data.split(":", 1)[1] if ":" in data else ""
        expected = ctx.user_data.pop("liveconfirm_token", None)
        if not token or token != expected:
            await q.edit_message_text(
                "⌛ This confirmation expired — run /mymode live again.",
                reply_markup=main_kb(uid),
            )
            return
        # Distinguish "no wallet" from "gate not ready" — the old combined check
        # rendered an empty reason list when the only problem was a missing key.
        if not (get_user_key(uid) or get_user_private_key(uid)):
            await q.edit_message_text(
                "🔴 No wallet connected — run /wallet\\_setup first.",
                reply_markup=main_kb(uid), parse_mode="Markdown",
            )
            return
        # Re-check the gate at confirm time — it may have flipped since the prompt.
        ready, reasons = _global_gate_check()
        if not ready:
            await q.edit_message_text(
                "🔴 Live no longer available:\n" + "\n".join(f"  · {r}" for r in reasons),
                reply_markup=main_kb(uid),
            )
            return
        # Provision the deposit wallet for live: deploy (if needed) + wrap any
        # USDC.e → pUSD + approve exchanges (all gasless). Skips cleanly for
        # users without a deposit wallet (legacy/paper). Never blocks the flip on
        # a transient relayer hiccup — surfaces a warning instead.
        from weather.secrets import get_user_creds, prepare_for_live
        _c = get_user_creds(uid) or {}
        prep_note = ""
        if _c.get("signature_type") == 3 and _c.get("funder_address"):
            await q.edit_message_text("⏳ Preparing your deposit wallet for live…")
            import asyncio
            # Record the funded USDC.e as a real deposit BEFORE prepare_for_live
            # wraps it to pUSD — otherwise a user who funded but never ran /deposit
            # would have a $0 deposit basis and an infinite/blank live return.
            try:
                from weather import relayer, live_ledger
                _usdce = await asyncio.to_thread(relayer.usdce_balance, _c["funder_address"])
                live_ledger.reconcile_deposit(_live_wallet_file(uid), _usdce)
            except Exception:
                pass
            prep = await asyncio.to_thread(prepare_for_live, uid)
            if prep.get("error"):
                prep_note = f"\n⚠️ Wallet prep incomplete: {prep['error'][:120]}"
            elif prep.get("pusd", 0) <= 0:
                prep_note = "\n⚠️ Deposit wallet has $0 pUSD — fund it (USDC.e) before orders can fill."
            else:
                prep_note = f"\n💰 Deposit wallet ready: ${prep['pusd']:,.2f} pUSD."
        with _users_transaction() as users:
            users[uid]["mode"] = "live"
            users[uid]["live_confirmed_at"] = datetime.utcnow().isoformat()
            # First-ever go-live: marks the start of the real-money track record.
            users[uid].setdefault("went_live_at", users[uid]["live_confirmed_at"])
        audit_log("mode_live", actor=uid)
        if uid != ADMIN_ID:
            await notify_owner(ctx.bot, f"🟢 User `{uid}` switched to LIVE trading.")
        await q.edit_message_text(
            "🟢 *Mode set to LIVE.* Orders execute on the next scheduled scan.\n"
            "Switch back any time with `/mymode paper`." + prep_note,
            reply_markup=main_kb(uid), parse_mode="Markdown",
        )
    elif data.startswith("withdrawconfirm"):
        if not has_permission(uid, perms.WITHDRAW_OWN):
            await q.edit_message_text("🚫 Your role can't withdraw.", reply_markup=main_kb(uid))
            return
        token = data.split(":", 1)[1] if ":" in data else ""
        pending = ctx.user_data.pop("withdraw_pending", None)
        if not pending or token != pending.get("token"):
            await q.edit_message_text(
                "⌛ This withdrawal confirmation expired — run /withdraw again.",
                reply_markup=main_kb(uid),
            )
            return
        # Re-check policy at confirm time — allowlist or daily-cap state may have
        # changed since the prompt (code gate already satisfied, so code_ok=True).
        recheck = wpol.evaluate_withdrawal(
            pending["amount"], pending["to"], get_withdraw_allowlist(uid),
            withdrawn_today(uid), get_withdraw_cap(uid), code_ok=True,
        )
        if not recheck.allowed:
            await q.edit_message_text(f"🚫 {recheck.reason}", reply_markup=main_kb(uid))
            return
        from weather.secrets import get_user_creds
        from weather import relayer
        creds = get_user_creds(uid) or {}
        wallet = creds.get("funder_address")
        if not wallet or not creds.get("pk"):
            await q.edit_message_text("🔴 No deposit wallet — cannot withdraw.", reply_markup=main_kb(uid))
            return
        await q.edit_message_text("⏳ Submitting on-chain withdrawal…")
        try:
            import asyncio
            rc = relayer.RelayerClient(pk=creds["pk"])
            result = await asyncio.to_thread(
                rc.unwrap_pusd_to_usdce, wallet, pending["amount_raw"], pending["to"]
            )
            txh = ""
            if isinstance(result, dict):
                txh = result.get("transactionHash", "")
            append_wallet_transaction(uid, "withdraw", pending["amount"],
                                      f"on-chain USDC.e to {pending['to']}")
            audit_log("withdrawal", actor=uid, amount=pending["amount"],
                      to=pending["to"], tx=txh or None)
            await notify_owner(
                ctx.bot,
                f"💸 Withdrawal executed\nuser `{uid}` · ${pending['amount']:,.2f} → `{pending['to']}`"
                + (f"\ntx `{txh}`" if txh else ""),
            )
            msg = (f"✅ *Withdrawal sent:* ${pending['amount']:,.2f} USDC.e → `{pending['to']}`")
            if txh:
                msg += f"\ntx: `{txh}`"
            await q.edit_message_text(msg, reply_markup=main_kb(uid), parse_mode="Markdown")
        except Exception as e:
            await q.edit_message_text(
                f"🔴 Withdrawal failed: {str(e)[:200]}", reply_markup=main_kb(uid)
            )
    elif data.startswith("why:"):
        trades_csv = _active_trades_csv_path(uid)
        rows: list[dict] = []
        if trades_csv.exists():
            with trades_csv.open() as f:
                rows = list(csv.DictReader(f))
        row = telegram_views.find_trade(rows, data[4:])
        if row is None:
            await q.edit_message_text("❌ Trade not found.", reply_markup=main_kb(uid))
        else:
            await q.edit_message_text(
                telegram_views.fmt_why(row), reply_markup=back_kb(), parse_mode="Markdown"
            )
    elif data == "scan" and has_permission(uid, perms.TRIGGER_SCAN):
        await q.edit_message_text(
            "🔍 *Trigger scan?*\nFetches live forecasts and evaluates all markets (~2–3 min).",
            reply_markup=confirm_kb("scan", "Run scan"),
            parse_mode="Markdown",
        )
    elif data == "scan_confirm" and has_permission(uid, perms.TRIGGER_SCAN):
        await q.edit_message_text("🔍 Scanning Polymarket...")
        stdout, stderr, rc = await run_bot_async("paper", uid)
        if rc == -2:
            await q.edit_message_text(f"⏳ {stderr}", reply_markup=main_kb(uid))
        elif rc != 0:
            err = (stderr or stdout or "Unknown error")[-300:].strip()
            await q.edit_message_text(
                f"❌ Scan failed\n```\n{err}\n```", reply_markup=main_kb(uid), parse_mode="Markdown"
            )
        else:
            summary = next((l for l in stdout.splitlines() if "evaluated" in l), "Scan complete.")
            await q.edit_message_text(f"✅ {summary.strip()}", reply_markup=main_kb(uid))
    elif data == "resolve" and has_permission(uid, perms.TRIGGER_SCAN):
        await q.edit_message_text(
            "✅ *Trigger auto-resolve?*\nResolves all pending paper trades that are due.",
            reply_markup=confirm_kb("resolve", "Run resolve"),
            parse_mode="Markdown",
        )
    elif data == "resolve_confirm" and has_permission(uid, perms.TRIGGER_SCAN):
        await q.edit_message_text("⏳ Running auto-resolve...")
        stdout, stderr, rc = await run_bot_async("auto-resolve", uid)
        if rc == -2:
            await q.edit_message_text(f"⏳ {stderr}", reply_markup=main_kb(uid))
        elif rc != 0:
            err = (stderr or stdout or "Unknown error")[-300:].strip()
            await q.edit_message_text(
                f"❌ Resolve failed\n```\n{err}\n```", reply_markup=main_kb(uid), parse_mode="Markdown"
            )
        else:
            summary = next((l for l in stdout.splitlines() if "Auto-resolved" in l), "Done.")
            await q.edit_message_text(f"✅ {summary.strip()}", reply_markup=main_kb(uid))
    elif data == "users" and has_permission(uid, perms.MANAGE_USERS):
        users = _load_users()
        lines = ["*👥 Registered Users*\n"]
        for u_id, info in users.items():
            lines.append(f"`{u_id}` — {info['role']} (@{info.get('username') or '?'})")
        await q.edit_message_text("\n".join(lines), reply_markup=back_kb(), parse_mode="Markdown")


# ── Push alerts ────────────────────────────────────────────────────────────────

_seen_signals_mtimes:  dict[int, float] = {}
_seen_resolved_mtimes: dict[int, float] = {}
_seen_halt_mtimes:     dict[int, float] = {}
_seen_alarm_mtime: float = 0.0
# Halt files older than bot start are history, not news — don't replay on restart.
_BOT_START_TS = datetime.now().timestamp()
_SCANNER_ALARM_LOG = DATA_DIR / "logs" / "scanner_alarm.csv"

def _read_json_if_new(path: Path, seen: dict[int, float], uid: int) -> "tuple[dict, float] | None":
    """Return (parsed_json, mtime) only when `path` is newer than the last seen
    mtime for uid, advancing the seen-mtime on a successful read. Returns None
    when the file is absent, unchanged, or unreadable — a corrupt/half-written
    file keeps the old mtime so it's retried next cycle instead of being lost."""
    if not path.exists():
        return None
    mtime = path.stat().st_mtime
    if mtime <= seen.get(uid, 0.0):
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    seen[uid] = mtime
    return data, mtime


async def check_alerts(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    bot = ctx.bot

    # Scanner health alarms (zero markets / parse-rate collapse) → admin only.
    # The E4 regression ran silently for 7 days because nothing read this file.
    global _seen_alarm_mtime
    if _SCANNER_ALARM_LOG.exists():
        mtime = _SCANNER_ALARM_LOG.stat().st_mtime
        if mtime > _seen_alarm_mtime:
            first_check = _seen_alarm_mtime == 0.0
            _seen_alarm_mtime = mtime
            if not first_check:  # don't replay old alarms on bot restart
                try:
                    with open(_SCANNER_ALARM_LOG) as f:
                        rows = list(csv.DictReader(f))
                    last = rows[-1] if rows else {}
                    await bot.send_message(
                        ADMIN_ID,
                        f"🚨 *Scanner alarm*\n"
                        f"Reason: `{last.get('reason', '?')}`\n"
                        f"Source: {last.get('source', '?')}  ·  {last.get('timestamp', '')[:16]}\n"
                        f"_Check logs/scanner\\_alarm.csv_",
                        parse_mode="Markdown",
                    )
                except Exception:
                    pass

    for uid in all_user_ids():
        signals_file  = _signals_path(uid)
        resolved_file = _resolved_path(uid)

        # Live-halt alert: weather_bot writes live_halt.json when a user's live
        # execution stops (kill switch, missing key, balance failure). Tell the
        # user AND the admin — silence here means money sits idle unnoticed.
        halt_new = _read_json_if_new(user_log_dir(uid) / "live_halt.json", _seen_halt_mtimes, uid)
        if halt_new is not None and halt_new[1] >= _BOT_START_TS:
            halt = halt_new[0]
            try:
                text = (
                    f"⛔ *Live trading halted*\n"
                    f"Reason: {halt.get('reason', '?')}\n"
                    f"_{halt.get('halted_at', '')[:16]}_"
                )
                await bot.send_message(uid, text, parse_mode="Markdown")
                if uid != ADMIN_ID:
                    await bot.send_message(
                        ADMIN_ID, f"⛔ Live halt for user `{uid}`: "
                        f"{halt.get('reason', '?')}", parse_mode="Markdown",
                    )
            except Exception:
                pass

        # New signals alert. The admin gets the richer _auto_scan heartbeat instead,
        # so skip them here to avoid a double notification (mtime still advances below).
        sig_new = _read_json_if_new(signals_file, _seen_signals_mtimes, uid)
        if sig_new is not None and uid != ADMIN_ID:
            signals = [s for s in sig_new[0].get("signals", []) if s.get("edge_pp", 0) >= 0.30]
            if signals:
                text = (
                    f"🔔 *Scheduled scan — {len(signals)} signal(s)*\n\n"
                    f"{fmt_signals(signals, uid)}"
                )
                try:
                    await bot.send_message(uid, text, parse_mode="Markdown")
                except Exception:
                    pass

        # Resolved trades alert — enhanced with wallet context
        res_new = _read_json_if_new(resolved_file, _seen_resolved_mtimes, uid)
        if res_new is not None:
            resolved = res_new[0].get("resolved", [])
            if resolved:
                batch_pnl = sum(float(t.get("pnl_usd") or 0) for t in resolved)
                e         = "✅" if batch_pnl >= 0 else "❌"
                lines     = [f"{e} *{len(resolved)} trade(s) resolved* — *${batch_pnl:+.2f}*\n"]

                for t in resolved[:5]:
                    pnl   = float(t.get("pnl_usd") or 0)
                    em    = "✅" if pnl > 0 else "❌"
                    title = t.get("market_title", "?")
                    if " - " in title:
                        title = title.split(" - ", 1)[1]
                    lines.append(f"{em} ${pnl:+.2f} — _{title[:44]}_")
                if len(resolved) > 5:
                    lines.append(f"_...and {len(resolved) - 5} more_")

                # Running wallet summary
                ws = wallet_stats(uid)
                lines.append(f"\n📊 *All-time: ${ws['realized_pnl']:+,.2f}*")
                if ws["return_pct"] is not None:
                    sign = "+" if ws["return_pct"] >= 0 else ""
                    lines.append(f"   Return:  {sign}{ws['return_pct']:.1f}%")
                if abs(ws["pnl_today"]) > 0:
                    lines.append(f"   Today:   ${ws['pnl_today']:+.2f}")
                if ws["pending_count"] > 0:
                    lines.append(f"   Open:    {ws['pending_count']} positions · ${ws['deployed']:,.0f} deployed")

                text = "\n".join(line for line in lines if line)
                try:
                    await bot.send_message(uid, text, parse_mode="Markdown")
                except Exception:
                    pass


# ── Main ──────────────────────────────────────────────────────────────────────

_USER_COMMANDS = [
    ("help",        "❓ All commands & usage"),
    ("status",      "📊 Portfolio overview: return %, win rate, gates"),
    ("paperstats",  "📝 Paper/model track record (always available)"),
    ("wallet",      "💼 Balance, deployed capital & PnL breakdown"),
    ("positions",   "📍 Open trades grouped by date & city"),
    ("trades",      "📋 Last resolved trades"),
    ("signals",     "🎯 Signals from the last scan"),
    ("why",         "💡 Full reasoning behind a trade"),
    ("scanreport",  "🔬 Scan funnel: fetched → gates → taken"),
    ("losses",      "❌ Losing trades with causes"),
    ("deposit",     "💰 Record a deposit"),
    ("withdraw",    "💸 Withdraw real USDC.e to an allowlisted address"),
    ("allowlist",   "🔐 View withdrawal allowlist & daily cap"),
    ("allowlist_add",    "➕ Allowlist a withdrawal address"),
    ("allowlist_remove", "➖ Remove a withdrawal address"),
    ("scan",        "🔍 Trigger a market scan"),
    ("resolve",     "✅ Auto-resolve pending trades"),
    ("mymode",      "🔄 View or change trading mode (paper/live)"),
    ("wallet_setup","🔧 Create or connect your trading wallet"),
]

_ADMIN_COMMANDS = _USER_COMMANDS + [
    ("invite",      "🎟 Generate an invite link"),
    ("adduser",     "➕ Add a user by Telegram ID"),
    ("removeuser",  "➖ Remove a user"),
    ("setrole",     "🎚 Change a user's role"),
    ("suspend",     "🚫 Suspend a user"),
    ("unsuspend",   "✅ Restore a suspended user"),
    ("users",       "👥 List all registered users"),
    ("audit",       "🧾 Recent audit log (money/role/live events)"),
    ("setwithdrawcap", "🔐 Set a user's daily withdrawal cap"),
    ("setup",       "⚙️ Save credentials (legacy)"),
    ("setmaxbet",   "💰 Set max bet size per trade (e.g. /setmaxbet 50)"),
]

def _commands_for_role(role: str | None) -> list[tuple[str, str]]:
    """Command menu shown to a user, by role. Admins/owner see the admin set;
    everyone else sees the base user set."""
    return _ADMIN_COMMANDS if role in perms.ADMIN_ROLES else _USER_COMMANDS

async def handle_admin_upload(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin sends a data file as Telegram document → saved to DATA_DIR/logs/."""
    uid = update.effective_user.id
    if not is_admin(uid):
        return
    targets = {
        "paper_trades.csv":      DATA_DIR / "logs" / "paper_trades.csv",
        "calibration_log.csv":   DATA_DIR / "logs" / "calibration_log.csv",
        "historical_skill.json": DATA_DIR / "logs" / "historical_skill.json",
        "city_bias.csv":         DATA_DIR / "logs" / "city_bias.csv",
    }
    doc = update.effective_message.document
    if not doc or doc.file_name not in targets:
        await update.effective_message.reply_text(
            "📂 Send one of these files to upload it to the Railway volume:\n"
            + "\n".join(f"• `{n}`" for n in targets),
            parse_mode="Markdown",
        )
        return
    dest = targets[doc.file_name]
    dest.parent.mkdir(parents=True, exist_ok=True)
    tg_file = await ctx.bot.get_file(doc.file_id)
    await tg_file.download_to_drive(str(dest))
    await update.effective_message.reply_text(
        f"✅ `{doc.file_name}` saved ({doc.file_size:,} bytes)\n`{dest}`",
        parse_mode="Markdown",
    )


def _count_trades(uid: int) -> int:
    """Row count of a user's paper-trades CSV (excludes the header)."""
    p = _trades_csv_path(uid)
    if not p.exists():
        return 0
    try:
        with p.open() as f:
            return max(0, sum(1 for _ in f) - 1)
    except OSError:
        return 0


async def _auto_scan(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Scheduled job: paper scan + fan-out to all users. Runs every AUTO_SCAN_INTERVAL seconds."""
    before = _count_trades(ADMIN_ID)
    stdout, stderr, rc = await run_bot_async("paper", ADMIN_ID)
    if rc == -2:
        return  # a scan/resolve was already running — skip silently
    if rc != 0:
        try:
            await ctx.bot.send_message(
                ADMIN_ID,
                f"⚠️ Auto-scan failed\n```\n{(stderr or stdout)[-300:].strip()}\n```",
                parse_mode="Markdown",
            )
        except Exception:
            pass
        return

    # Success → heartbeat summary so every scheduled scan is visible, even quiet ones.
    new_trades = max(0, _count_trades(ADMIN_ID) - before)
    meta       = read_last_scan_meta(ADMIN_ID)
    try:
        funnel = json.loads(_signals_path(ADMIN_ID).read_text()).get("funnel", {})
    except Exception:
        funnel = {}
    header = [f"🔍 *Scan complete* · _{meta.get('scanned_at', '')}_"]
    if funnel.get("evaluated") is not None:
        header.append(f"Evaluated: {funnel['evaluated']} · Actionable: {funnel.get('actionable', 0)}")
    header.append(f"New paper trades: *{new_trades}*")
    body = fmt_signals(read_last_signals(ADMIN_ID), ADMIN_ID)
    try:
        await ctx.bot.send_message(
            ADMIN_ID, "\n".join(header) + "\n\n" + body, parse_mode="Markdown",
        )
    except Exception:
        pass


async def _auto_resolve(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Scheduled job: auto-resolve pending trades. Runs every AUTO_RESOLVE_INTERVAL seconds."""
    stdout, stderr, rc = await run_bot_async("auto-resolve", ADMIN_ID)
    if rc not in (0, -2):
        try:
            await ctx.bot.send_message(
                ADMIN_ID,
                f"⚠️ Auto-resolve failed\n```\n{(stderr or stdout)[-300:].strip()}\n```",
                parse_mode="Markdown",
            )
        except Exception:
            pass


async def _register_commands(app) -> None:
    from telegram import BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeChat
    # Default menu (unknown / new chats) = base user commands.
    user_cmds = [BotCommand(c, d) for c, d in _USER_COMMANDS]
    await app.bot.set_my_commands(user_cmds, scope=BotCommandScopeAllPrivateChats())
    # Per-user override: owner/admin get the admin menu in their own chat.
    # (Non-admins keep the default base menu, so no per-user call needed.)
    for uid in all_user_ids():
        if get_role(uid) not in perms.ADMIN_ROLES:
            continue
        cmds = [BotCommand(c, d) for c, d in _commands_for_role(get_role(uid))]
        try:
            await app.bot.set_my_commands(cmds, scope=BotCommandScopeChat(uid))
        except Exception:
            pass  # user hasn't started the chat yet — updates on next restart


async def _run() -> None:
    _seed_volume_data()
    _seed_admin()
    _seed_admin_creds()
    _migrate_global_wallet()
    _log_startup()

    app = Application.builder().token(BOT_TOKEN).build()

    # Onboarding conversation owns /start and /wallet_setup. It must be added
    # BEFORE the generic CallbackQueryHandler so its ob_* buttons are consumed
    # by the active conversation, not by on_button.
    import telegram_onboarding
    app.add_handler(telegram_onboarding.build_conversation_handler())
    app.add_handler(CommandHandler("invite", telegram_onboarding.cmd_invite))

    for cmd, handler in [
        ("help",        cmd_help),
        ("status",      cmd_status),
        ("paperstats",  cmd_paperstats),
        ("wallet",      cmd_wallet),
        ("positions",   cmd_positions),
        ("signals",     cmd_signals),
        ("trades",      cmd_trades),
        ("why",         cmd_why),
        ("scanreport",  cmd_scanreport),
        ("losses",      cmd_losses),
        ("scan",        cmd_scan),
        ("resolve",     cmd_resolve),
        ("setup",       cmd_setup),
        ("exportkey",   cmd_exportkey),
        ("mymode",      cmd_mymode),
        ("deposit",     cmd_deposit),
        ("withdraw",    cmd_withdraw),
        ("allowlist",        cmd_allowlist),
        ("allowlist_add",    cmd_allowlist_add),
        ("allowlist_remove", cmd_allowlist_remove),
        ("users",       cmd_users),
        ("audit",       cmd_audit),
        ("adduser",     cmd_adduser),
        ("removeuser",  cmd_removeuser),
        ("setrole",     cmd_setrole),
        ("suspend",     cmd_suspend),
        ("unsuspend",   cmd_unsuspend),
        ("setwithdrawcap", cmd_setwithdrawcap),
        ("setmaxbet",   cmd_setmaxbet),
    ]:
        app.add_handler(CommandHandler(cmd, handler))

    app.add_handler(MessageHandler(
        filters.Document.ALL & filters.ChatType.PRIVATE,
        handle_admin_upload,
    ))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_error_handler(_on_error)

    async with app:
        await app.start()
        await _register_commands(app)
        await _run_self_check(app)
        app.job_queue.run_repeating(check_alerts, interval=120, first=10)

        # Automatic scan + resolve — replace launchd on Railway.
        # Override intervals via env vars; set to 0 to disable.
        scan_interval    = int(os.environ.get("AUTO_SCAN_INTERVAL",    "14400"))  # 4h
        resolve_interval = int(os.environ.get("AUTO_RESOLVE_INTERVAL", "3600"))   # 1h
        if scan_interval > 0:
            app.job_queue.run_repeating(_auto_scan,    interval=scan_interval,    first=300)
        if resolve_interval > 0:
            app.job_queue.run_repeating(_auto_resolve, interval=resolve_interval, first=600)

        await app.updater.start_polling(drop_pending_updates=True)
        print("Polymarket Bot online.", flush=True)
        await asyncio.Event().wait()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
