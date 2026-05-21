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
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
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
TRADES_CSV    = ROOT / "logs" / "paper_trades.csv"
SIGNALS_FILE  = ROOT / "logs" / "last_signals.json"
RESOLVED_FILE = ROOT / "logs" / "last_resolved.json"
USERS_FILE    = ROOT / "config" / "users.json"

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ["POLYMARKET_BOT_TOKEN"]
ADMIN_ID  = int(os.environ["TELEGRAM_ADMIN_ID"])
PYTHON    = str(ROOT / "venv" / "bin" / "python")


# ── User registry ─────────────────────────────────────────────────────────────

def _load_users() -> dict[int, dict]:
    if not USERS_FILE.exists():
        return {}
    return {int(k): v for k, v in json.loads(USERS_FILE.read_text()).items()}

def _save_users(users: dict[int, dict]) -> None:
    USERS_FILE.parent.mkdir(exist_ok=True)
    USERS_FILE.write_text(json.dumps({str(k): v for k, v in users.items()}, indent=2))

def _seed_admin() -> None:
    users = _load_users()
    if ADMIN_ID not in users:
        users[ADMIN_ID] = {
            "role": "admin",
            "username": "owner",
            "added_at": datetime.utcnow().isoformat(),
        }
        _save_users(users)

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


# ── Stats ─────────────────────────────────────────────────────────────────────

def read_stats() -> dict:
    if not TRADES_CSV.exists():
        return {}
    with TRADES_CSV.open() as f:
        rows = list(csv.DictReader(f))

    resolved = [r for r in rows if r.get("resolved_at")]
    wins   = [r for r in resolved if r.get("pnl_usd") and float(r["pnl_usd"]) > 0]
    losses = [r for r in resolved if r.get("pnl_usd") and float(r["pnl_usd"]) < 0]

    gross_win  = sum(float(r["pnl_usd"]) for r in wins)
    gross_loss = abs(sum(float(r["pnl_usd"]) for r in losses))
    pf         = gross_win / gross_loss if gross_loss > 0 else None
    total_pnl  = sum(float(r["pnl_usd"]) for r in resolved if r.get("pnl_usd"))

    brier     = [float(r["brier_score"]) for r in resolved if r.get("brier_score")]
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

def read_last_signals() -> list[dict]:
    if not SIGNALS_FILE.exists():
        return []
    return json.loads(SIGNALS_FILE.read_text()).get("signals", [])


# ── Formatters ────────────────────────────────────────────────────────────────

def fmt_status(s: dict) -> str:
    if not s:
        return "No trade data yet."
    lines = ["*📊 Paper Trading Status*\n"]
    lines.append(f"Trades:       {s['resolved']} resolved / {s['pending']} pending")
    if s["win_rate"] is not None:
        lines.append(f"Win rate:     {s['win_rate']:.1f}%  ({s['wins']}W / {s['losses']}L)")
    if s["profit_factor"] is not None:
        e = "✅" if s["profit_factor"] >= 1.5 else "⚠️"
        lines.append(f"Prof. factor: {e} {s['profit_factor']:.2f}  (need ≥ 1.5)")
    if s["bss"] is not None:
        lines.append(f"Brier skill:  {s['bss']:+.3f}  (need ≥ 0.0)")
    lines.append(f"Total PnL:    *€{s['total_pnl']:.2f}*")
    if s["pending_by_date"]:
        lines.append("\n_Pending resolves:_")
        for d, n in s["pending_by_date"].items():
            lines.append(f"  {d}: {n} trade(s)")
    gate = "🟢 ALL GATES PASSED — ready for live" if s["gates_passed"] else "🔴 Gates not yet passed"
    lines.append(f"\n{gate}")
    return "\n".join(lines)

def fmt_signals(signals: list[dict]) -> str:
    if not signals:
        return "No signals from last scan."
    scanned_at = ""
    if SIGNALS_FILE.exists():
        scanned_at = json.loads(SIGNALS_FILE.read_text()).get("scanned_at", "")[:16].replace("T", " ")
    lines = [f"*🎯 Last Scan Signals* — {len(signals)} total"]
    if scanned_at:
        lines.append(f"_Scanned: {scanned_at} UTC_\n")
    for s in signals[:10]:
        d = "📈 YES" if s["direction"] == "YES" else "📉 NO"
        edge = s.get("edge_pp", 0)
        model_p = s.get("model_p", 0)
        mkt_p = s.get("mkt_p", 0)
        title = s.get("title", "?")[:50]
        lines.append(f"{d} *{edge:.0%}* edge — _{title}_")
        lines.append(f"   Model {model_p:.1%} vs Mkt {mkt_p:.1%}")
    if len(signals) > 10:
        lines.append(f"\n_...and {len(signals) - 10} more_")
    return "\n".join(lines)

def fmt_trades(n: int = 10) -> str:
    if not TRADES_CSV.exists():
        return "No trades yet."
    with TRADES_CSV.open() as f:
        rows = list(csv.DictReader(f))
    resolved = sorted(
        [r for r in rows if r.get("resolved_at")],
        key=lambda x: x.get("resolved_at", ""), reverse=True
    )[:n]
    if not resolved:
        return "No resolved trades yet."
    lines = [f"*📋 Last {len(resolved)} Resolved Trades*\n"]
    for r in resolved:
        pnl = float(r.get("pnl_usd", 0))
        e = "✅" if pnl > 0 else "❌"
        title = r.get("market_title", "?")[:44]
        direction = r.get("direction", "?")
        edge = float(r.get("edge_pp", 0))
        lines.append(f"{e} *€{pnl:+.2f}* | {direction} | {edge:.0%} edge")
        lines.append(f"   _{title}_")
    return "\n".join(lines)


# ── Keyboards ─────────────────────────────────────────────────────────────────

def main_kb(uid: int) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("📊 Status",  callback_data="status"),
            InlineKeyboardButton("🎯 Signals", callback_data="signals"),
        ],
        [InlineKeyboardButton("📋 Trades", callback_data="trades")],
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


# ── Helpers ───────────────────────────────────────────────────────────────────

def run_bot(mode: str) -> str:
    """Run weather_bot.py in a given mode, return stdout."""
    args = [PYTHON, "weather_bot.py", "--mode", mode]
    if mode == "paper":
        args += ["--interval", "0"]
    r = subprocess.run(args, capture_output=True, text=True, cwd=ROOT, timeout=180)
    return r.stdout


# ── Command handlers ───────────────────────────────────────────────────────────

@require_auth()
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    role = get_role(uid)
    await update.message.reply_text(
        f"👋 *Polymarket Bot*\nRole: `{role}`\n\nUse the buttons or type /help.",
        reply_markup=main_kb(uid),
        parse_mode="Markdown",
    )

@require_auth()
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lines = [
        "*Available commands:*\n",
        "/status — trading stats",
        "/signals — last scan signals",
        "/trades [n] — last N resolved trades",
    ]
    if is_admin(uid):
        lines += [
            "\n*Admin:*",
            "/scan — trigger a scan now",
            "/resolve — run auto-resolve",
            "/adduser <id> [admin|viewer] — add user",
            "/removeuser <id> — remove user",
            "/users — list users",
        ]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

@require_auth()
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        fmt_status(read_stats()),
        reply_markup=main_kb(update.effective_user.id),
        parse_mode="Markdown",
    )

@require_auth()
async def cmd_signals(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        fmt_signals(read_last_signals()),
        reply_markup=main_kb(update.effective_user.id),
        parse_mode="Markdown",
    )

@require_auth()
async def cmd_trades(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    n = 10
    if ctx.args:
        try:
            n = min(int(ctx.args[0]), 30)
        except ValueError:
            pass
    await update.effective_message.reply_text(
        fmt_trades(n),
        reply_markup=main_kb(update.effective_user.id),
        parse_mode="Markdown",
    )

@require_auth(admin_only=True)
async def cmd_scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.effective_message.reply_text("🔍 Scanning Polymarket...")
    out = run_bot("paper")
    summary = next((l for l in out.splitlines() if "evaluated" in l), "Scan complete.")
    await msg.edit_text(f"✅ {summary.strip()}", reply_markup=main_kb(update.effective_user.id))

@require_auth(admin_only=True)
async def cmd_resolve(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.effective_message.reply_text("⏳ Running auto-resolve...")
    out = run_bot("auto-resolve")
    summary = next((l for l in out.splitlines() if "Auto-resolved" in l), "Done.")
    await msg.edit_text(f"✅ {summary.strip()}", reply_markup=main_kb(update.effective_user.id))

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
    users = _load_users()
    users[new_id] = {"role": role, "username": "", "added_at": datetime.utcnow().isoformat()}
    _save_users(users)
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
    users = _load_users()
    if rm_id not in users:
        await update.message.reply_text("User not found.")
        return
    del users[rm_id]
    _save_users(users)
    await update.message.reply_text(f"✅ Removed `{rm_id}`.", parse_mode="Markdown")


# ── Callback handler (inline buttons) ─────────────────────────────────────────

@require_auth()
async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = update.effective_user.id
    data = q.data

    if data == "status":
        await q.edit_message_text(
            fmt_status(read_stats()), reply_markup=main_kb(uid), parse_mode="Markdown"
        )
    elif data == "signals":
        await q.edit_message_text(
            fmt_signals(read_last_signals()), reply_markup=main_kb(uid), parse_mode="Markdown"
        )
    elif data == "trades":
        await q.edit_message_text(
            fmt_trades(), reply_markup=main_kb(uid), parse_mode="Markdown"
        )
    elif data == "scan" and is_admin(uid):
        await q.edit_message_text("🔍 Scanning Polymarket...")
        out = run_bot("paper")
        summary = next((l for l in out.splitlines() if "evaluated" in l), "Scan complete.")
        await q.edit_message_text(f"✅ {summary.strip()}", reply_markup=main_kb(uid))
    elif data == "resolve" and is_admin(uid):
        await q.edit_message_text("⏳ Running auto-resolve...")
        out = run_bot("auto-resolve")
        summary = next((l for l in out.splitlines() if "Auto-resolved" in l), "Done.")
        await q.edit_message_text(f"✅ {summary.strip()}", reply_markup=main_kb(uid))
    elif data == "users" and is_admin(uid):
        users = _load_users()
        lines = ["*👥 Registered Users*\n"]
        for u_id, info in users.items():
            lines.append(f"`{u_id}` — {info['role']} (@{info.get('username') or '?'})")
        await q.edit_message_text("\n".join(lines), reply_markup=back_kb(), parse_mode="Markdown")


# ── Push alerts ────────────────────────────────────────────────────────────────

_seen_signals_mtime:  float = 0.0
_seen_resolved_mtime: float = 0.0

async def check_alerts(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    global _seen_signals_mtime, _seen_resolved_mtime
    bot = ctx.bot
    recipients = all_user_ids()

    if SIGNALS_FILE.exists():
        mtime = SIGNALS_FILE.stat().st_mtime
        if mtime > _seen_signals_mtime:
            _seen_signals_mtime = mtime
            data = json.loads(SIGNALS_FILE.read_text())
            signals = [s for s in data.get("signals", []) if s.get("edge_pp", 0) >= 0.30]
            if signals:
                text = f"🔔 *Scheduled scan — {len(signals)} signal(s)*\n\n{fmt_signals(signals)}"
                for uid in recipients:
                    try:
                        await bot.send_message(uid, text, parse_mode="Markdown")
                    except Exception:
                        pass

    if RESOLVED_FILE.exists():
        mtime = RESOLVED_FILE.stat().st_mtime
        if mtime > _seen_resolved_mtime:
            _seen_resolved_mtime = mtime
            data = json.loads(RESOLVED_FILE.read_text())
            resolved = data.get("resolved", [])
            if resolved:
                total = sum(t.get("pnl_usd", 0) for t in resolved)
                e = "✅" if total >= 0 else "❌"
                lines = [f"{e} *{len(resolved)} trade(s) resolved* — *€{total:+.2f}*\n"]
                for t in resolved[:5]:
                    pnl = t.get("pnl_usd", 0)
                    em = "✅" if pnl > 0 else "❌"
                    title = t.get("market_title", "?")[:44]
                    lines.append(f"{em} €{pnl:+.2f} — _{title}_")
                if len(resolved) > 5:
                    lines.append(f"_...and {len(resolved) - 5} more_")
                # Append fresh stats
                s = read_stats()
                if s:
                    lines.append(f"\n📊 PnL: *€{s['total_pnl']:.2f}* | PF: {s['profit_factor']:.2f}" if s.get("profit_factor") else "")
                text = "\n".join(l for l in lines if l)
                for uid in recipients:
                    try:
                        await bot.send_message(uid, text, parse_mode="Markdown")
                    except Exception:
                        pass


# ── Main ──────────────────────────────────────────────────────────────────────

async def _run() -> None:
    _seed_admin()

    app = Application.builder().token(BOT_TOKEN).build()

    for cmd, handler in [
        ("start",       cmd_start),
        ("help",        cmd_help),
        ("status",      cmd_status),
        ("signals",     cmd_signals),
        ("trades",      cmd_trades),
        ("scan",        cmd_scan),
        ("resolve",     cmd_resolve),
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
        await asyncio.Event().wait()  # run until process is killed


def main() -> None:
    import asyncio
    asyncio.run(_run())


if __name__ == "__main__":
    main()
