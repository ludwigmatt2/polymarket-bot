"""
Onboarding wizard + invite codes for the Telegram bot.

Flow (ConversationHandler, private chats only):
  /start <invite-code>        — registers the user, enters the wizard
  /wallet_setup               — re-entry point for already-registered users

  WALLET_CHOICE  🆕 create wallet  /  🔗 connect Polymarket account  /  ⏭ skip
  KEY_PASTE      (connect path) user pastes exported private key → encrypted store
  PROXY_PASTE    (connect path) user pastes Polymarket deposit/proxy address
  FUNDING        funding instructions + on-demand balance check

Invite codes live in config/invites.json; admin creates them with /invite.
Private keys only ever touch weather/secrets.py (keyring/Fernet) — never
users.json, never logs. Pasted-key messages are deleted from the chat.
"""

from __future__ import annotations

import asyncio
import json
import secrets as pysecrets
from datetime import datetime, timedelta
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from weather._io import atomic_write_text
from weather.config import MIN_PROFIT_FACTOR, MIN_RESOLVED_TRADES
from weather.paths import DATA_DIR
from weather.secrets import (
    derive_and_store_clob_creds,
    get_user_creds,
    get_user_key,
    set_user_creds,
    set_user_key,
)

INVITES_FILE = DATA_DIR / "config" / "invites.json"
INVITE_TTL_DAYS = 7

# Conversation states
WALLET_CHOICE, KEY_PASTE, PROXY_PASTE, FUNDING = range(4)


# ── Invite codes ──────────────────────────────────────────────────────────────

def _load_invites() -> dict:
    if not INVITES_FILE.exists():
        return {}
    try:
        return json.loads(INVITES_FILE.read_text())
    except json.JSONDecodeError:
        return {}


def _save_invites(invites: dict) -> None:
    INVITES_FILE.parent.mkdir(exist_ok=True)
    atomic_write_text(INVITES_FILE, json.dumps(invites, indent=2))


def create_invite(created_by: int, role: str = "viewer") -> str:
    code = pysecrets.token_urlsafe(8)
    invites = _load_invites()
    invites[code] = {
        "role": role,
        "created_by": created_by,
        "created_at": datetime.utcnow().isoformat(),
        "expires_at": (datetime.utcnow() + timedelta(days=INVITE_TTL_DAYS)).isoformat(),
        "used_by": None,
    }
    _save_invites(invites)
    return code


def validate_invite(code: str) -> dict | None:
    """Return the invite record if the code is valid, unused, and unexpired."""
    inv = _load_invites().get(code)
    if not inv or inv.get("used_by") is not None:
        return None
    if inv.get("expires_at", "") < datetime.utcnow().isoformat():
        return None
    return inv


def mark_invite_used(code: str, uid: int) -> None:
    invites = _load_invites()
    if code in invites:
        invites[code]["used_by"] = uid
        _save_invites(invites)


# ── Key validation / wallet generation ────────────────────────────────────────

def normalize_private_key(raw: str) -> str | None:
    """Validate a pasted private key; return canonical 0x-form or None."""
    key = raw.strip()
    if not key.startswith("0x"):
        key = "0x" + key
    try:
        from eth_account import Account
        Account.from_key(key)
        return key
    except Exception:
        return None


def is_eth_address(raw: str) -> bool:
    s = raw.strip()
    if not (s.startswith("0x") and len(s) == 42):
        return False
    try:
        int(s[2:], 16)
        return True
    except ValueError:
        return False


def generate_wallet() -> tuple[str, str]:
    """Fresh Polygon-compatible EOA. Returns (address, private_key_hex)."""
    from eth_account import Account
    acct = Account.create()
    return acct.address, acct.key.hex()


def fetch_user_balance_sync(uid: int, proxy: str | None, signature_type: str) -> float:
    """Blocking USDC balance fetch via the official CLOB SDK (same path as live trading).

    `proxy`/`signature_type` are kept for call-site compatibility, but the stored
    creds (funder_address + integer signature_type, legacy-migrated on read) are
    the source of truth.
    """
    creds = get_user_creds(uid)
    if not creds or not creds.get("pk"):
        raise RuntimeError("no key stored")
    from weather.live_trader import fetch_balance_for_creds
    return fetch_balance_for_creds(creds)


# ── users.json helpers (lazy import to avoid a circular module load) ─────────

def _tb():
    import telegram_bot
    return telegram_bot


def _set_user_fields(uid: int, **fields) -> None:
    tb = _tb()
    with tb._users_transaction() as users:
        users.setdefault(uid, {}).update(fields)


def register_user(uid: int, username: str, role: str, invited_by: int) -> None:
    tb = _tb()
    with tb._users_transaction() as users:
        users[uid] = {
            "role": role,
            "username": username or "",
            "added_at": datetime.utcnow().isoformat(),
            "mode": "paper",
            "proxy_address": None,
            "wallet_address": None,
            "wallet_type": "none",
            "signature_type": None,
            "invited_by": invited_by,
        }


# ── Wizard keyboards / texts ──────────────────────────────────────────────────

def _choice_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🆕 Create a wallet for me", callback_data="ob_create")],
        [InlineKeyboardButton("🔗 Connect my Polymarket account", callback_data="ob_connect")],
        [InlineKeyboardButton("⏭ Skip for now (paper only)", callback_data="ob_skip")],
    ])


def _funding_kb(show_reveal: bool) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("💰 Check balance", callback_data="ob_balance")]]
    if show_reveal:
        rows.append([InlineKeyboardButton("🔑 Reveal key for backup (60s)", callback_data="ob_reveal")])
    rows.append([InlineKeyboardButton("✅ Done", callback_data="ob_done")])
    return InlineKeyboardMarkup(rows)


_FUNDING_TEXT = (
    "*💸 Fund your wallet*\n\n"
    "Send *USDC.e on the Polygon network* to:\n`{address}`\n\n"
    "⚠️ Important:\n"
    "• Use the *Polygon* network, not Ethereum\n"
    "• Polymarket uses *USDC.e* (bridged USDC) — native USDC may not be tradeable\n"
    "• Also send a little *POL* (~0.2) for transaction gas\n\n"
    "Then tap *Check balance*. You start in *paper mode* either way — "
    "no real money moves until you explicitly run /mymode live."
)

_DONE_TEXT = (
    "*✅ You're set up!*\n\n"
    "The bot scans weather markets twice daily with a shared forecast model and "
    "mirrors every signal to your account in *paper mode* (no real money).\n\n"
    "*Going live:* the shared model must hold its track record "
    f"(≥{MIN_RESOLVED_TRADES} resolved trades at profit factor ≥{MIN_PROFIT_FACTOR}), "
    "and you confirm with /mymode live.\n\n"
    "Useful commands: /status · /wallet · /positions · /scanreport · /help"
)


# ── Handlers ──────────────────────────────────────────────────────────────────

async def onboarding_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry: /start [invite-code]. Owns all /start handling."""
    tb = _tb()
    uid = update.effective_user.id

    if tb.is_authorized(uid):
        role = tb.get_role(uid)
        mode = tb.get_mode(uid)
        await update.message.reply_text(
            f"👋 *Polymarket Weather Bot*\n"
            f"Role: `{role}` · Mode: {mode}\n\n"
            f"The bot scans weather prediction markets twice daily, runs a calibrated "
            f"ensemble forecast model, and mirrors signals to your account.\n\n"
            f"Use the buttons below or type /help for all commands.",
            reply_markup=tb.main_kb(uid),
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    code = ctx.args[0] if ctx.args else None
    inv = validate_invite(code) if code else None
    if inv is None:
        await update.message.reply_text(
            "🚫 Not authorized.\n"
            "Ask the owner for an invite link (they create one with /invite)."
        )
        return ConversationHandler.END

    register_user(uid, update.effective_user.username or "", inv["role"], inv["created_by"])
    mark_invite_used(code, uid)

    await update.message.reply_text(
        "*👋 Welcome!* You're registered.\n\n"
        "To trade with your own money later, the bot needs a Polygon wallet for you. "
        "Pick an option — you can always do this later with /wallet\\_setup:",
        reply_markup=_choice_kb(),
        parse_mode="Markdown",
    )
    return WALLET_CHOICE


async def wallet_setup_entry(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Re-entry for registered users: /wallet_setup."""
    tb = _tb()
    uid = update.effective_user.id
    if not tb.is_authorized(uid):
        await update.message.reply_text("🚫 Not authorized.")
        return ConversationHandler.END
    await update.message.reply_text(
        "*🔧 Wallet setup*\nPick an option:",
        reply_markup=_choice_kb(),
        parse_mode="Markdown",
    )
    return WALLET_CHOICE


async def on_wallet_choice(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    uid = update.effective_user.id

    if q.data == "ob_skip":
        await q.edit_message_text(_DONE_TEXT, parse_mode="Markdown")
        return ConversationHandler.END

    if q.data == "ob_create":
        address, pk = generate_wallet()
        try:
            set_user_creds(uid, pk=pk, proxy_address=None, signature_type="eoa")
        except RuntimeError as exc:
            await q.edit_message_text(f"❌ Key storage unavailable: {exc}")
            return ConversationHandler.END
        finally:
            del pk
        _set_user_fields(
            uid,
            wallet_address=address,
            wallet_type="generated-eoa",
            signature_type="eoa",
            proxy_address=None,
            onboarded_at=datetime.utcnow().isoformat(),
        )
        await q.edit_message_text(
            "*🆕 Wallet created* (key stored encrypted on the bot machine)\n\n"
            + _FUNDING_TEXT.format(address=address),
            reply_markup=_funding_kb(show_reveal=True),
            parse_mode="Markdown",
        )
        return FUNDING

    # ob_connect
    await q.edit_message_text(
        "*🔗 Connect your Polymarket account*\n\n"
        "1. Open polymarket.com → click your profile → *Settings*\n"
        "2. Find *Export Private Key* and copy it\n"
        "3. Paste the key here as a normal message\n\n"
        "🔒 It's stored encrypted and your message is deleted immediately.\n"
        "Type /cancel to abort.",
        parse_mode="Markdown",
    )
    return KEY_PASTE


async def on_key_paste(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id
    key = normalize_private_key(update.message.text or "")

    # Scrub + delete regardless of validity — the chat held a (possible) secret.
    try:
        await update.message.delete()
        deleted = True
    except Exception:
        deleted = False

    if key is None:
        await update.effective_chat.send_message(
            "❌ That doesn't look like a valid private key. Paste it again, or /cancel."
        )
        return KEY_PASTE

    try:
        set_user_creds(uid, pk=key)
    except RuntimeError as exc:
        await update.effective_chat.send_message(f"❌ Key storage unavailable: {exc}")
        return ConversationHandler.END
    finally:
        del key

    warn = "" if deleted else (
        "\n⚠️ I couldn't delete your message — *please delete it yourself now*."
    )
    await update.effective_chat.send_message(
        "✅ Key stored (encrypted)." + warn + "\n\n"
        "Now paste your *Polymarket deposit address* (shown on your portfolio page, "
        "starts with 0x). This is the proxy wallet that holds your funds.\n"
        "If you trade with a plain wallet (no Polymarket UI account), send *none*.",
        parse_mode="Markdown",
    )
    return PROXY_PASTE


async def on_proxy_paste(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id
    text = (update.message.text or "").strip()

    if text.lower() in ("none", "no", "skip"):
        set_user_creds(uid, signature_type="eoa")
        _set_user_fields(
            uid,
            wallet_type="imported-eoa",
            signature_type="eoa",
            proxy_address=None,
            onboarded_at=datetime.utcnow().isoformat(),
        )
    elif is_eth_address(text):
        set_user_creds(uid, proxy_address=text, signature_type="gnosis-safe")
        _set_user_fields(
            uid,
            wallet_address=text,
            wallet_type="polymarket-proxy",
            signature_type="gnosis-safe",
            proxy_address=text,
            onboarded_at=datetime.utcnow().isoformat(),
        )
    else:
        await update.message.reply_text(
            "❌ Not a valid address (need 0x + 40 hex chars). Paste again, send *none*, or /cancel.",
            parse_mode="Markdown",
        )
        return PROXY_PASTE

    await update.message.reply_text("✅ Account connected. Deriving trading credentials...")
    try:
        await asyncio.wait_for(
            asyncio.to_thread(derive_and_store_clob_creds, uid),
            timeout=20,
        )
        await update.effective_chat.send_message("🔑 L2 credentials derived and stored.")
    except Exception as exc:
        await update.effective_chat.send_message(
            f"⚠️ Could not derive L2 credentials ({type(exc).__name__}). "
            "Balance checks may show $0. Run /wallet\\_setup again to retry.",
            parse_mode="Markdown",
        )
    await _balance_reply(update.effective_chat, uid)
    await update.effective_chat.send_message(_DONE_TEXT, parse_mode="Markdown")
    return ConversationHandler.END


async def _balance_reply(chat, uid: int) -> None:
    tb = _tb()
    user = tb._load_users().get(uid, {})
    try:
        balance = await asyncio.wait_for(
            asyncio.to_thread(
                fetch_user_balance_sync, uid,
                user.get("proxy_address"), user.get("signature_type") or "eoa",
            ),
            timeout=30,
        )
    except Exception as exc:
        await chat.send_message(
            f"⚠️ Balance check failed ({type(exc).__name__}). "
            "Funds may still be in transit — try /wallet\\_setup → Check balance later.",
            parse_mode="Markdown",
        )
        return
    if balance > 0:
        # First sighting of real funds → seed the per-user ledger.
        ws = tb.wallet_stats(uid)
        if ws["deposited"] == 0:
            tb.append_wallet_transaction(uid, "deposit", balance, "onboarding funding detected")
        await chat.send_message(f"💰 Balance: *${balance:,.2f} USDC*", parse_mode="Markdown")
    else:
        await chat.send_message("💰 Balance: $0.00 — no funds detected yet.")


async def on_funding_action(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    uid = update.effective_user.id

    if q.data == "ob_balance":
        await _balance_reply(update.effective_chat, uid)
        return FUNDING

    if q.data == "ob_reveal":
        creds = get_user_creds(uid)
        pk = creds.get("pk") if creds else None
        if not pk:
            await update.effective_chat.send_message("❌ No key stored.")
            return FUNDING
        msg = await update.effective_chat.send_message(
            f"🔑 *Your private key* (auto-deletes in 60s — back it up NOW):\n`{pk}`",
            parse_mode="Markdown",
        )
        ctx.job_queue.run_once(
            _delete_message_job, 60,
            data={"chat_id": msg.chat_id, "message_id": msg.message_id},
        )
        return FUNDING

    # ob_done
    await q.edit_message_text(_DONE_TEXT, parse_mode="Markdown")
    return ConversationHandler.END


async def _delete_message_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await ctx.bot.delete_message(ctx.job.data["chat_id"], ctx.job.data["message_id"])
    except Exception:
        pass


async def on_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Setup cancelled. Run /wallet_setup any time to continue."
    )
    return ConversationHandler.END


# ── /invite (admin command, lives outside the conversation) ──────────────────

async def cmd_invite(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tb = _tb()
    uid = update.effective_user.id
    if not tb.is_admin(uid):
        await update.message.reply_text("🚫 Admin only.")
        return
    role = "viewer"
    if ctx.args and ctx.args[0] in ("admin", "viewer"):
        role = ctx.args[0]
    code = create_invite(created_by=uid, role=role)
    bot_username = (await ctx.bot.get_me()).username
    await update.message.reply_text(
        f"🎟 *Invite created* (role: `{role}`, valid {INVITE_TTL_DAYS} days, single-use)\n\n"
        f"Share this link:\nhttps://t.me/{bot_username}?start={code}",
        parse_mode="Markdown",
    )


# ── Registration ──────────────────────────────────────────────────────────────

def build_conversation_handler() -> ConversationHandler:
    private = filters.ChatType.PRIVATE
    return ConversationHandler(
        entry_points=[
            CommandHandler("start", onboarding_start, filters=private),
            CommandHandler("wallet_setup", wallet_setup_entry, filters=private),
        ],
        states={
            WALLET_CHOICE: [CallbackQueryHandler(on_wallet_choice, pattern="^ob_")],
            KEY_PASTE: [MessageHandler(private & filters.TEXT & ~filters.COMMAND, on_key_paste)],
            PROXY_PASTE: [MessageHandler(private & filters.TEXT & ~filters.COMMAND, on_proxy_paste)],
            FUNDING: [CallbackQueryHandler(on_funding_action, pattern="^ob_")],
        },
        fallbacks=[CommandHandler("cancel", on_cancel)],
        conversation_timeout=600,
        per_chat=True,
    )
