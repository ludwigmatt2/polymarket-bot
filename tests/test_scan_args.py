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


class TestIntradayLiveWiring:
    """Intraday live execution (Aug 23 2026) is a standalone opt-in, not implied
    by overall /mymode live the way hourly is — both the toggle AND overall
    live mode must be on, so it can be turned off independently if the track's
    numbers turn bad without killing hourly live trading too."""

    def test_no_live_flag_when_intraday_toggle_off(self, monkeypatch):
        monkeypatch.setattr(tb, "get_user_mode", lambda uid: "live")
        monkeypatch.setattr(tb, "get_intraday_live", lambda uid: False)
        assert "--live" not in tb._scan_args("intraday")

    def test_no_live_flag_when_overall_mode_paper(self, monkeypatch):
        monkeypatch.setattr(tb, "get_user_mode", lambda uid: "paper")
        monkeypatch.setattr(tb, "get_intraday_live", lambda uid: True)
        assert "--live" not in tb._scan_args("intraday")

    def test_live_flag_only_when_both_on(self, monkeypatch):
        monkeypatch.setattr(tb, "get_user_mode", lambda uid: "live")
        monkeypatch.setattr(tb, "get_intraday_live", lambda uid: True)
        assert "--live" in tb._scan_args("intraday")
