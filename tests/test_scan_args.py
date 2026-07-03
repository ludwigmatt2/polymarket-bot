"""The scheduled/manual scan must run one-shot, and a *live* root scan is what
actually places the admin's live orders (fan-out excludes the admin)."""
import os

os.environ.setdefault("POLYMARKET_BOT_TOKEN", "test:token")
os.environ.setdefault("TELEGRAM_ADMIN_ID", "1")

import telegram_bot as tb


def _arg(args, flag):
    return args[args.index(flag) + 1] if flag in args else None


def test_live_scan_is_one_shot_live_mode():
    args = tb._scan_args("live")
    assert _arg(args, "--mode") == "live"
    assert _arg(args, "--interval") == "0"   # one-shot, else weather_bot loops forever
    assert "--all-users" in args


def test_paper_scan_is_one_shot():
    assert _arg(tb._scan_args("paper"), "--interval") == "0"


def test_resolve_modes_are_not_forced_one_shot():
    # resolve/auto-resolve are already loop-free; they must not get --interval 0 bolted on
    assert "--interval" not in tb._scan_args("auto-resolve")
