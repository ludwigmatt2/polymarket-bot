"""PR5 — command audit fixes: scan/resolve permission gate + mode-scoped views."""

import asyncio
import csv as _csv
import os
import threading
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("POLYMARKET_BOT_TOKEN", "test:token")
os.environ.setdefault("TELEGRAM_ADMIN_ID", "1")

import telegram_bot as tb


def _run(make_coro):
    box: dict = {}

    def runner():
        try:
            box["v"] = asyncio.run(make_coro())
        except BaseException as e:  # noqa: BLE001
            box["e"] = e

    t = threading.Thread(target=runner)
    t.start()
    t.join()
    if "e" in box:
        raise box["e"]
    return box["v"]


class TestActiveTradesPath:
    def test_follows_mode(self, monkeypatch):
        monkeypatch.setattr(tb, "get_user_mode", lambda uid: "live")
        assert tb._active_trades_csv_path(5) == tb._live_trades_csv_path(5)
        monkeypatch.setattr(tb, "get_user_mode", lambda uid: "paper")
        assert tb._active_trades_csv_path(5) == tb._trades_csv_path(5)


class TestScanPermissionGate:
    def _neutralize_auth(self, monkeypatch, has_perm):
        monkeypatch.setattr(tb, "_ensure_authorized", AsyncMock(return_value=True))
        monkeypatch.setattr(tb, "has_permission", lambda uid, cap: has_perm)
        monkeypatch.setattr(tb, "run_bot_async", AsyncMock(return_value=("", "", 0)))

    def test_scan_refused_without_trigger_scan(self, monkeypatch):
        self._neutralize_auth(monkeypatch, has_perm=False)
        update = MagicMock()
        update.effective_user.id = 5
        update.effective_message.reply_text = AsyncMock()
        _run(lambda: tb.cmd_scan(update, MagicMock()))
        tb.run_bot_async.assert_not_called()                 # scan never triggered
        assert "permission" in update.effective_message.reply_text.call_args.args[0].lower()

    def test_resolve_refused_without_trigger_scan(self, monkeypatch):
        self._neutralize_auth(monkeypatch, has_perm=False)
        update = MagicMock()
        update.effective_user.id = 5
        update.effective_message.reply_text = AsyncMock()
        _run(lambda: tb.cmd_resolve(update, MagicMock()))
        tb.run_bot_async.assert_not_called()


class TestScanOrderIssueSurfacing:
    """A live order can fail without failing the scan itself (rc=0) — the
    ORDER_ISSUE marker must reach the user even on a nominally successful
    /scan, not just get folded into the quiet '✅ Scan complete' text
    (Aug 23 2026: a correctly-sized live order vanished with zero trace)."""

    def _setup(self, monkeypatch, stdout):
        monkeypatch.setattr(tb, "_ensure_authorized", AsyncMock(return_value=True))
        monkeypatch.setattr(tb, "has_permission", lambda uid, cap: True)
        monkeypatch.setattr(tb, "run_bot_async", AsyncMock(return_value=(stdout, "", 0)))
        monkeypatch.setattr(tb, "get_user_mode", lambda uid: "live")
        update = MagicMock()
        update.effective_user.id = 5
        msg = MagicMock()
        msg.edit_text = AsyncMock()
        update.effective_message.reply_text = AsyncMock(return_value=msg)
        return update, msg

    def test_order_issue_surfaced_on_successful_scan(self, monkeypatch):
        stdout = ("5 evaluated | 1 actionable | 4 rejected\n"
                  "ORDER_ISSUE: Lowest temperature in Seoul | ValueError: bad signature")
        update, msg = self._setup(monkeypatch, stdout)
        _run(lambda: tb.cmd_scan(update, MagicMock()))
        text = msg.edit_text.call_args.args[0]
        assert "Live order issue" in text
        assert "bad signature" in text

    def test_no_issue_block_when_scan_clean(self, monkeypatch):
        stdout = "5 evaluated | 0 actionable | 5 rejected"
        update, msg = self._setup(monkeypatch, stdout)
        _run(lambda: tb.cmd_scan(update, MagicMock()))
        text = msg.edit_text.call_args.args[0]
        assert "Live order issue" not in text


class TestAutoScanHeartbeatSurfacesBlock:
    """The scheduled _auto_scan push alert for a live-order block is de-spammed
    to once/day — which once let a persistent geoblock hide behind quiet
    '0 trades' heartbeats for days (Aug 2026: VPS egressed over an IPv6 address
    geolocated to a blocked region). The heartbeat fires every scan, so an
    unresolved block must be surfaced there too, un-mutably."""

    def _setup(self, monkeypatch, stdout):
        monkeypatch.setattr(tb, "_wrap_pending_live_deposits", lambda: [])
        monkeypatch.setattr(tb, "run_bot_async", AsyncMock(return_value=(stdout, "", 0)))
        monkeypatch.setattr(tb, "_count_trades", lambda uid: 0)
        monkeypatch.setattr(tb, "read_last_scan_meta", lambda uid: {"scanned_at": "2026-08-25T13:33"})
        monkeypatch.setattr(tb, "read_last_signals", lambda uid: [])
        monkeypatch.setattr(tb, "fmt_signals", lambda sigs, uid: "(no signals)")
        monkeypatch.setattr(tb, "get_user_mode", lambda uid: "live")
        bad_path = MagicMock()
        bad_path.read_text.side_effect = OSError  # funnel read → {} (try/except)
        monkeypatch.setattr(tb, "_signals_path", lambda uid: bad_path)
        ctx = MagicMock()
        ctx.bot.send_message = AsyncMock()
        ctx.bot_data = {}
        return ctx

    def test_geoblock_shown_in_heartbeat(self, monkeypatch):
        stdout = ("406 evaluated | 2 actionable | 404 rejected\n"
                  "ORDER_ISSUE: geoblocked from DE/BY (IP 2a01:4f9::1) — route order "
                  "traffic through a permitted region (set HTTPS_PROXY).")
        ctx = self._setup(monkeypatch, stdout)
        _run(lambda: tb._auto_scan(ctx))
        # Last send is the heartbeat (the first is the de-spammed push alert).
        heartbeat = ctx.bot.send_message.call_args_list[-1].args[1]
        assert "Orders blocked this scan" in heartbeat
        assert "geoblocked" in heartbeat

    def test_clean_scan_has_no_block_line(self, monkeypatch):
        ctx = self._setup(monkeypatch, "406 evaluated | 0 actionable | 406 rejected")
        _run(lambda: tb._auto_scan(ctx))
        heartbeat = ctx.bot.send_message.call_args_list[-1].args[1]
        assert "Orders blocked this scan" not in heartbeat


class TestLiveDegradationEscalation:
    """Regular ORDER_ISSUE alerts mute at 1/day — the same pattern that hid the
    Aug 25 2026 IPv6 geoblock for days. "live trader unavailable this cycle"
    (weather_bot.py's _build_live_trader_or_none) means live trading is fully
    sidelined, not just one blocked order, so it gets a time-based escalation
    on top of the regular per-day mute."""

    def _ctx(self):
        ctx = MagicMock()
        ctx.bot_data = {}
        return ctx

    def test_no_issues_clears_state_and_never_escalates(self):
        ctx = self._ctx()
        ctx.bot_data["hourly_live_degraded_since"] = tb.datetime.now(tb.timezone.utc) - tb.timedelta(hours=48)
        assert tb._track_live_degradation(ctx, "hourly", []) is None
        assert "hourly_live_degraded_since" not in ctx.bot_data

    def test_unrelated_order_issue_does_not_count_toward_escalation(self):
        ctx = self._ctx()
        issues = ["ORDER_ISSUE: Seoul market | ValueError: bad signature"]
        assert tb._track_live_degradation(ctx, "hourly", issues) is None
        assert "hourly_live_degraded_since" not in ctx.bot_data

    def test_first_occurrence_records_timestamp_but_does_not_escalate(self):
        ctx = self._ctx()
        issues = ["ORDER_ISSUE: live trader unavailable this cycle | RuntimeError: boom"]
        assert tb._track_live_degradation(ctx, "hourly", issues) is None
        assert "hourly_live_degraded_since" in ctx.bot_data

    def test_escalates_once_threshold_elapsed(self):
        ctx = self._ctx()
        issues = ["ORDER_ISSUE: live trader unavailable this cycle | RuntimeError: boom"]
        ctx.bot_data["hourly_live_degraded_since"] = tb.datetime.now(tb.timezone.utc) - tb.timedelta(hours=13)
        msg = tb._track_live_degradation(ctx, "hourly", issues)
        assert msg is not None and "13h" in msg

    def test_does_not_re_escalate_before_re_escalate_window(self):
        ctx = self._ctx()
        issues = ["ORDER_ISSUE: live trader unavailable this cycle | RuntimeError: boom"]
        ctx.bot_data["hourly_live_degraded_since"] = tb.datetime.now(tb.timezone.utc) - tb.timedelta(hours=13)
        assert tb._track_live_degradation(ctx, "hourly", issues) is not None
        # Same cycle, immediately after — still within the 6h re-escalate window.
        assert tb._track_live_degradation(ctx, "hourly", issues) is None

    def test_re_escalates_after_re_escalate_window(self):
        ctx = self._ctx()
        issues = ["ORDER_ISSUE: live trader unavailable this cycle | RuntimeError: boom"]
        ctx.bot_data["hourly_live_degraded_since"] = tb.datetime.now(tb.timezone.utc) - tb.timedelta(hours=20)
        ctx.bot_data["hourly_live_escalated_at"] = tb.datetime.now(tb.timezone.utc) - tb.timedelta(hours=7)
        msg = tb._track_live_degradation(ctx, "hourly", issues)
        assert msg is not None

    def test_hourly_and_intraday_tracks_are_independent(self):
        ctx = self._ctx()
        issues = ["ORDER_ISSUE: live trader unavailable this cycle | RuntimeError: boom"]
        ctx.bot_data["hourly_live_degraded_since"] = tb.datetime.now(tb.timezone.utc) - tb.timedelta(hours=13)
        hourly_msg = tb._track_live_degradation(ctx, "hourly", issues)
        intraday_msg = tb._track_live_degradation(ctx, "intraday", issues)
        assert hourly_msg is not None
        assert intraday_msg is None  # intraday's own streak just started


class TestMainlineHourlyIntradaySplit:
    """Mainline blends hourly + intraday into one number for the go-live gate —
    by design, that number can't tell you whether intraday is pulling its
    weight or riding on hourly's performance. read_stats() must expose both
    standalone, without changing what the gate itself reads (Aug 23 2026)."""

    def _write_trades(self, tmp_path, rows):
        path = tmp_path / "paper_trades.csv"
        fieldnames = ["scan_source", "signal_time", "resolved_at", "pnl_usd", "brier_score"]
        with open(path, "w", newline="") as f:
            w = _csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for r in rows:
                w.writerow(r)
        return path

    def _row(self, scan_source, pnl):
        return {
            "scan_source": scan_source,
            "signal_time": "2026-08-10T00:00:00+00:00",  # after GATE_ERA_START
            "resolved_at": "2026-08-11T00:00:00+00:00",
            "pnl_usd": str(pnl),
            "brier_score": "",
        }

    def test_hourly_and_intraday_split_independently_from_blended_mainline(self, tmp_path, monkeypatch):
        rows = [
            self._row("hourly", 10),      # hourly: 1W
            self._row("hourly", -5),      # hourly: 1L
            self._row("intraday", -8),    # intraday: 1L
            self._row("intraday", -3),    # intraday: 1L
            self._row("longshot", 999),   # must not leak into either bucket
        ]
        self._write_trades(tmp_path, rows)
        monkeypatch.setattr(tb, "user_data_dir", lambda uid: tmp_path)

        s = tb.read_stats(1)
        mh = s["tracks"]["mainline_hourly"]
        mi = s["tracks"]["mainline_intraday"]
        m = s["tracks"]["mainline"]

        assert mh["resolved"] == 2 and mh["wins"] == 1 and mh["losses"] == 1
        assert mi["resolved"] == 2 and mi["wins"] == 0 and mi["losses"] == 2
        assert mi["total_pnl"] == -11
        # blended mainline still combines both, unchanged — the gate reads this
        assert m["resolved"] == 4
        assert m["total_pnl"] == mh["total_pnl"] + mi["total_pnl"]


class TestModeScopedViews:
    def test_trades_reads_live_and_badges_when_live(self, tmp_path, monkeypatch):
        # signal_time must be within the current era — _relevant_trades() (Aug
        # 20 2026 fix) scopes every listing view the same way wallet_stats() does.
        live_csv = tmp_path / "live_trades.csv"
        with open(live_csv, "w", newline="") as f:
            w = _csv.DictWriter(f, fieldnames=["trade_id", "market_title", "signal_time",
                                               "resolved_at", "pnl_usd"])
            w.writeheader()
            w.writerow({"trade_id": "L1", "market_title": "Live market",
                        "signal_time": "2026-08-10T00:00:00",
                        "resolved_at": "2026-08-10T00:00:00", "pnl_usd": "4"})
        monkeypatch.setattr(tb, "get_user_mode", lambda uid: "live")
        monkeypatch.setattr(tb, "_active_trades_csv_path", lambda uid: live_csv)
        out = tb.fmt_trades(9)
        assert "Live" in out and "Live market" in out

    def test_positions_excludes_errored_live_rows(self, tmp_path, monkeypatch):
        live_csv = tmp_path / "live_trades.csv"
        with open(live_csv, "w", newline="") as f:
            w = _csv.DictWriter(f, fieldnames=["market_title", "signal_time", "resolved_at",
                                               "size_usd", "error", "resolution_date", "edge_pp"])
            w.writeheader()
            w.writerow({"market_title": "Open ok", "signal_time": "2026-08-10T00:00:00",
                        "size_usd": "10", "error": "",
                        "resolution_date": "2026-07-09", "edge_pp": "0.2"})
            w.writerow({"market_title": "Errored", "signal_time": "2026-08-10T00:00:00",
                        "size_usd": "5",
                        "error": "insufficient balance", "resolution_date": "2026-07-09", "edge_pp": "0.2"})
        monkeypatch.setattr(tb, "_active_trades_csv_path", lambda uid: live_csv)
        out = tb.fmt_positions(9)
        assert "1 trades" in out          # only the non-errored position counts
        assert "$10 deployed" in out


class TestMuteSignature:
    """The daily ORDER_ISSUE mute promises "repeats muted today unless the error
    changes". Order IDs are nonce-derived hashes, unique on every submission, so
    keying on the raw text made every repeat look like a NEW error and the alert
    fired on every scan (Aug 27 2026: FAK-kill 400s paged continuously)."""

    def _err(self, order_id: str) -> str:
        return ("ORDER_ISSUE: Highest temperature in Atlanta on August 28? | "
                "PolyApiException[status_code=400, error_message={'error': 'no "
                "orders found to match with FAK order.', 'orderID': '%s'}]" % order_id)

    def test_same_failure_different_order_id_dedupes(self):
        a = tb._mute_signature(self._err("0xa80d4d0bf94cc2abc80d83b37ccf3496"))
        b = tb._mute_signature(self._err("0x21167f07c360e366948de7531c696afc"))
        assert a == b

    def test_different_failure_still_alerts(self):
        a = tb._mute_signature(self._err("0xa80d4d0bf94cc2abc80d83b37ccf3496"))
        b = tb._mute_signature("ORDER_ISSUE: Atlanta | ValueError: bad signature")
        assert a != b

    def test_different_market_still_alerts(self):
        a = tb._mute_signature("ORDER_ISSUE: Atlanta | boom 0xdeadbeefcafe1234")
        b = tb._mute_signature("ORDER_ISSUE: Seoul | boom 0xdeadbeefcafe1234")
        assert a != b

    def test_short_hex_is_not_collapsed(self):
        """Only long identifiers are volatile; don't blur real content."""
        assert tb._mute_signature("code 0x1f") == "code 0x1f"
