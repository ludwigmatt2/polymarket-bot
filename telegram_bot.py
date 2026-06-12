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
from weather.secrets import get_user_key, set_user_key
import telegram_views
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

load_dotenv()

# ── Paths ─────────────────────────────────────────────────────────────────────
ROOT          = Path(__file__).parent
USERS_FILE    = ROOT / "config" / "users.json"
WALLET_FILE   = ROOT / "logs" / "wallet.json"

def user_log_dir(uid: int) -> Path:
    return ROOT / "logs" / "users" / str(uid)

def user_trades_csv(uid: int) -> Path:
    return user_log_dir(uid) / "paper_trades.csv"

def user_signals_file(uid: int) -> Path:
    return user_log_dir(uid) / "last_signals.json"

def user_resolved_file(uid: int) -> Path:
    return user_log_dir(uid) / "last_resolved.json"

# The admin's data IS the root logs/ (the scan runs there; fan-out mirrors to
# everyone else). Non-admin users never fall back to root — a fresh user must
# see an empty slate, not the owner's portfolio.
def _trades_csv_path(uid: int) -> Path:
    return ROOT / "logs" / "paper_trades.csv" if uid == ADMIN_ID else user_trades_csv(uid)

def _signals_path(uid: int) -> Path:
    return ROOT / "logs" / "last_signals.json" if uid == ADMIN_ID else user_signals_file(uid)

def _resolved_path(uid: int) -> Path:
    return ROOT / "logs" / "last_resolved.json" if uid == ADMIN_ID else user_resolved_file(uid)


# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ["POLYMARKET_BOT_TOKEN"]
ADMIN_ID  = int(os.environ["TELEGRAM_ADMIN_ID"])
PYTHON    = str(ROOT / "venv" / "bin" / "python")


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
    atomic_write_text(USERS_FILE, json.dumps({str(k): v for k, v in users.items()}, indent=2))
    _users_cache = users

@contextmanager
def _users_transaction():
    """Load, yield the mutable users dict, then save — all under the lock."""
    with _users_lock:
        users = _load_users()
        yield users
        _save_users(users)

def _seed_admin() -> None:
    with _users_lock:
        users = _load_users()
        if ADMIN_ID not in users:
            users[ADMIN_ID] = {
                "role": "admin",
                "username": "owner",
                "added_at": datetime.utcnow().isoformat(),
                "private_key": None,
                "proxy_address": None,
                "mode": "paper",
            }
            _save_users(users)

def get_user_mode(uid: int) -> str:
    return _load_users().get(uid, {}).get("mode", "paper")

def get_user_private_key(uid: int) -> Optional[str]:
    return _load_users().get(uid, {}).get("private_key")

def get_role(user_id: int) -> Optional[str]:
    return _load_users().get(user_id, {}).get("role")

def is_admin(user_id: int) -> bool:
    return get_role(user_id) == "admin"

def is_authorized(user_id: int) -> bool:
    return get_role(user_id) in ("admin", "viewer")

def all_user_ids() -> list[int]:
    return list(_load_users().keys())


# ── Auth decorator ────────────────────────────────────────────────────────────

def require_auth(admin_only: bool = False):
    def decorator(fn):
        async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
            uid = update.effective_user.id
            if not is_authorized(uid):
                await update.effective_message.reply_text(
                    "🚫 Not authorized. Ask an admin to add you with /adduser."
                )
                return
            if admin_only and not is_admin(uid):
                await update.effective_message.reply_text("🚫 Admin only.")
                return
            return await fn(update, ctx)
        return wrapper
    return decorator


# ── Wallet (per-user ledger; audit C3) ───────────────────────────────────────

def user_wallet_file(uid: int) -> Path:
    return user_log_dir(uid) / "wallet.json"

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
    atomic_write_text(f, json.dumps(data, indent=2))

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
    csv_path = _trades_csv_path(uid)
    if not csv_path.exists():
        return "No open positions."

    with csv_path.open() as f:
        rows = list(csv.DictReader(f))

    pending = [r for r in rows if not r.get("resolved_at")]
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
    trades_csv = _trades_csv_path(uid)
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
    lines = [f"*📋 Last {len(resolved)} Resolved Trades*\n"]
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
    trades_csv = _trades_csv_path(uid)
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
    if is_admin(uid):
        rows.append([
            InlineKeyboardButton("🔍 Scan",    callback_data="scan"),
            InlineKeyboardButton("✅ Resolve", callback_data="resolve"),
        ])
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
        "/wallet — balance, deployed, PnL breakdown",
        "/positions — open trades grouped by date & city",
        "/trades [n] — last N resolved trades (default 10)",
        "/signals — signals from the last scan\n",
        "*Transparency*",
        "/why <trade_id> — full reasoning behind a trade",
        "/scanreport — scan funnel: what was fetched, rejected, taken",
        "/losses [n] — losing trades with causes\n",
        "*Wallet tracking*",
        "/deposit <amount> [note] — record a deposit",
        "/withdraw <amount> [note] — record a withdrawal\n",
        "*Bot control*",
        "/scan — trigger a market scan",
        "/resolve — auto-resolve pending paper trades",
        "/mymode [paper|live] — view or change mode",
        "/wallet\\_setup — create or connect your trading wallet",
    ]
    if is_admin(uid):
        lines += [
            "\n*Admin*",
            "/invite [admin|viewer] — create an invite link",
            "/adduser <id> [admin|viewer] — add user",
            "/removeuser <id> — remove user",
            "/users — list users",
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
    trades_csv = _trades_csv_path(uid)
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
    trades_csv = _trades_csv_path(uid)
    rows: list[dict] = []
    if trades_csv.exists():
        with trades_csv.open() as f:
            rows = list(csv.DictReader(f))
    await update.effective_message.reply_text(
        telegram_views.fmt_losses(rows, n), reply_markup=main_kb(uid), parse_mode="Markdown",
    )

@require_auth()
async def cmd_deposit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Record a deposit. Usage: /deposit <amount> [note]"""
    if not ctx.args:
        await update.message.reply_text(
            "Usage: `/deposit <amount> [note]`\nExample: `/deposit 500 initial load`",
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
    append_wallet_transaction(update.effective_user.id, "deposit", amount, note)
    ws = wallet_stats(update.effective_user.id)
    await update.message.reply_text(
        f"✅ *Deposit recorded:* +${amount:,.2f}\n"
        f"Total deposited: ${ws['deposited']:,.2f}\n"
        f"Wallet balance:  ${ws['wallet_balance']:,.2f}",
        parse_mode="Markdown",
    )

@require_auth()
async def cmd_withdraw(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Record a withdrawal. Usage: /withdraw <amount> [note]"""
    if not ctx.args:
        await update.message.reply_text(
            "Usage: `/withdraw <amount> [note]`\nExample: `/withdraw 200 partial exit`",
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
    append_wallet_transaction(update.effective_user.id, "withdraw", amount, note)
    ws = wallet_stats(update.effective_user.id)
    await update.message.reply_text(
        f"✅ *Withdrawal recorded:* -${amount:,.2f}\n"
        f"Total withdrawn: ${ws['withdrawn']:,.2f}\n"
        f"Wallet balance:  ${ws['wallet_balance']:,.2f}",
        parse_mode="Markdown",
    )

@require_auth()
async def cmd_scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
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

@require_auth()
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

@require_auth(admin_only=True)
async def cmd_setup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not ctx.args:
        await update.message.reply_text(
            "*Setup your Polymarket account*\n\n"
            "Usage: `/setup <private_key> [proxy_address]`\n\n"
            "Your private key is stored locally in `config/users.json`. "
            "Send this command in a private chat with the bot.",
            parse_mode="Markdown",
        )
        return
    # Store key encrypted; scrub the plaintext arg from memory immediately
    raw_key = ctx.args[0]
    ctx.args[0] = "***"
    try:
        set_user_key(uid, raw_key)
    except RuntimeError as exc:
        await update.message.reply_text(
            f"❌ Key storage unavailable: {exc}\n"
            "Set `POLYMARKET_SECRETS_KEY` in `.env` or install `keyring`.",
            parse_mode="Markdown",
        )
        return

    if len(ctx.args) > 1:
        with _users_transaction() as users:
            users[uid]["proxy_address"] = ctx.args[1]

    # Best-effort: delete the user's message to remove the key from chat history
    try:
        await update.message.delete()
        key_deleted = True
    except Exception:
        key_deleted = False

    reply = (
        "✅ Credentials saved (encrypted). Use `/mymode live` to switch to live trading.\n\n"
    )
    if not key_deleted:
        reply += (
            "⚠️ *Please delete your message containing the private key* — "
            "it remains visible in this chat and may be stored by Telegram."
        )
    await update.message.reply_text(reply, parse_mode="Markdown")

def _global_gate_check() -> tuple[bool, list[str]]:
    """One model, one track record: live unlocks on the ROOT paper log's gates."""
    from weather.paper_trader import PaperTrader as _PT
    stats = _PT(log_path=ROOT / "logs" / "paper_trades.csv").compute_stats()
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

    # live: global model gate + own credentials + explicit risk confirmation
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
    await update.message.reply_text(
        "⚠️ *Switching to LIVE trading*\n\n"
        "The bot will place real orders with *your* USDC on every scheduled scan, "
        "sized by Kelly fraction against your balance. Losses are real.\n\n"
        "Confirm?",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Yes, trade my real money", callback_data="liveconfirm"),
            InlineKeyboardButton("❌ Cancel", callback_data="status"),
        ]]),
        parse_mode="Markdown",
    )

@require_auth(admin_only=True)
async def cmd_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    users = _load_users()
    lines = ["*👥 Registered Users*\n"]
    for uid, info in users.items():
        lines.append(f"`{uid}` — {info['role']} (@{info.get('username') or '?'})")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

@require_auth(admin_only=True)
async def cmd_adduser(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: /adduser <telegram_id> [admin|viewer]")
        return
    try:
        new_id = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid ID — must be a number.")
        return
    role = "viewer"
    if len(ctx.args) > 1 and ctx.args[1] in ("admin", "viewer"):
        role = ctx.args[1]
    with _users_transaction() as users:
        users[new_id] = {
            "role": role, "username": "", "added_at": datetime.utcnow().isoformat(),
            "private_key": None, "proxy_address": None, "mode": "paper",
        }
    await update.message.reply_text(
        f"✅ Added `{new_id}` as `{role}`.", parse_mode="Markdown"
    )

@require_auth(admin_only=True)
async def cmd_removeuser(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: /removeuser <telegram_id>")
        return
    try:
        rm_id = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid ID.")
        return
    if rm_id == ADMIN_ID:
        await update.message.reply_text("❌ Cannot remove the primary admin.")
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
    await update.message.reply_text(f"✅ Removed `{rm_id}`.", parse_mode="Markdown")


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
    elif data == "liveconfirm":
        # Re-check the gate at confirm time — it may have flipped since the prompt.
        ready, reasons = _global_gate_check()
        if not (get_user_key(uid) or get_user_private_key(uid)) or not ready:
            await q.edit_message_text(
                "🔴 Live no longer available:\n" + "\n".join(f"  · {r}" for r in reasons),
                reply_markup=main_kb(uid),
            )
        else:
            with _users_transaction() as users:
                users[uid]["mode"] = "live"
                users[uid]["live_confirmed_at"] = datetime.utcnow().isoformat()
            await q.edit_message_text(
                "🟢 *Mode set to LIVE.* Orders execute on the next scheduled scan.\n"
                "Switch back any time with `/mymode paper`.",
                reply_markup=main_kb(uid), parse_mode="Markdown",
            )
    elif data.startswith("why:"):
        trades_csv = _trades_csv_path(uid)
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
    elif data == "scan" and is_admin(uid):
        await q.edit_message_text(
            "🔍 *Trigger scan?*\nFetches live forecasts and evaluates all markets (~2–3 min).",
            reply_markup=confirm_kb("scan", "Run scan"),
            parse_mode="Markdown",
        )
    elif data == "scan_confirm":
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
    elif data == "resolve" and is_admin(uid):
        await q.edit_message_text(
            "✅ *Trigger auto-resolve?*\nResolves all pending paper trades that are due.",
            reply_markup=confirm_kb("resolve", "Run resolve"),
            parse_mode="Markdown",
        )
    elif data == "resolve_confirm":
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
    elif data == "users" and is_admin(uid):
        users = _load_users()
        lines = ["*👥 Registered Users*\n"]
        for u_id, info in users.items():
            lines.append(f"`{u_id}` — {info['role']} (@{info.get('username') or '?'})")
        await q.edit_message_text("\n".join(lines), reply_markup=back_kb(), parse_mode="Markdown")


# ── Push alerts ────────────────────────────────────────────────────────────────

_seen_signals_mtimes:  dict[int, float] = {}
_seen_resolved_mtimes: dict[int, float] = {}
_seen_alarm_mtime: float = 0.0
_SCANNER_ALARM_LOG = Path("logs/scanner_alarm.csv")

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

        # New signals alert
        if signals_file.exists():
            mtime = signals_file.stat().st_mtime
            if mtime > _seen_signals_mtimes.get(uid, 0.0):
                _seen_signals_mtimes[uid] = mtime
                data    = json.loads(signals_file.read_text())
                signals = [s for s in data.get("signals", []) if s.get("edge_pp", 0) >= 0.30]
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
        if resolved_file.exists():
            mtime = resolved_file.stat().st_mtime
            if mtime > _seen_resolved_mtimes.get(uid, 0.0):
                _seen_resolved_mtimes[uid] = mtime
                data     = json.loads(resolved_file.read_text())
                resolved = data.get("resolved", [])
                if resolved:
                    batch_pnl = sum(t.get("pnl_usd", 0) for t in resolved)
                    e         = "✅" if batch_pnl >= 0 else "❌"
                    lines     = [f"{e} *{len(resolved)} trade(s) resolved* — *${batch_pnl:+.2f}*\n"]

                    for t in resolved[:5]:
                        pnl   = t.get("pnl_usd", 0)
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

async def _run() -> None:
    _seed_admin()
    _migrate_global_wallet()

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
        ("mymode",      cmd_mymode),
        ("deposit",     cmd_deposit),
        ("withdraw",    cmd_withdraw),
        ("users",       cmd_users),
        ("adduser",     cmd_adduser),
        ("removeuser",  cmd_removeuser),
    ]:
        app.add_handler(CommandHandler(cmd, handler))

    app.add_handler(CallbackQueryHandler(on_button))

    async with app:
        await app.start()
        app.job_queue.run_repeating(check_alerts, interval=120, first=10)
        await app.updater.start_polling(drop_pending_updates=True)
        print("Polymarket Bot online.", flush=True)
        await asyncio.Event().wait()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
