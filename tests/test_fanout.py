"""Tests for the scan-once fan-out: per-user mirroring without touching the model.

The architecture invariant under test: one scan, one model, universal calibration.
Fan-out only mirrors already-computed signals to per-user log dirs, and per-user
resolves must NEVER feed the calibration log (model=None).
"""

import csv
import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

import weather_bot
from weather.models import (
    EnsembleForecast,
    Location,
    RawProbabilityResult,
    Signal,
    WeatherMarket,
)
from weather.paper_trader import PaperTrader

ADMIN_ID = "111"
USER_A = "222"
USER_B = "333"


def _make_signal(market_id: str = "mkt-1", gate_passed: bool = True) -> Signal:
    loc = Location(city="NYC", lat=40.7, lon=-74.0, timezone="America/New_York")
    market = WeatherMarket(
        market_id=market_id,
        title=f"Highest temperature in NYC? ({market_id})",
        yes_price=0.60,
        liquidity_usd=5000.0,
        resolution_date=datetime(2026, 6, 20, tzinfo=timezone.utc),
        resolution_source="NOAA",
        location=loc,
        metric="temperature_2m_max",
        threshold=30.0,
        direction="above",
        url="https://example.com",
    )
    prob = RawProbabilityResult(
        raw_p=0.20, calibrated_p=0.25, ensemble_spread=0.05, n_members=60,
        is_calibrated=False, model_breakdown={"gfs_seamless": 0.22}, threshold=30.0,
        direction="above", metric="temperature_2m_max",
    )
    forecast = EnsembleForecast(
        lat=40.7, lon=-74.0, target_date=date(2026, 6, 20),
        metric="temperature_2m_max",
    )
    return Signal(
        market=market, model_p=0.25, market_p=0.60, edge_pp=0.35, direction="NO",
        ensemble_spread=0.05, confidence_score=0.8, size_factor=1.0,
        quality_gate_passed=gate_passed, rejection_reason=None,
        signal_time=datetime.now(timezone.utc), forecast=forecast, prob_result=prob,
    )


@pytest.fixture
def fanout_env(tmp_path, monkeypatch):
    """Isolated users.json + logs dir + admin env for fan-out runs."""
    users_file = tmp_path / "config" / "users.json"
    users_file.parent.mkdir()
    users_file.write_text(json.dumps({
        ADMIN_ID: {"role": "admin", "mode": "paper"},
        USER_A: {"role": "viewer", "mode": "paper"},
        USER_B: {"role": "viewer", "mode": "paper"},
    }))
    monkeypatch.setattr(weather_bot, "USERS_FILE", users_file)
    monkeypatch.setenv("TELEGRAM_ADMIN_ID", ADMIN_ID)
    monkeypatch.chdir(tmp_path)  # fan-out writes to logs/users/<uid>/ relative to cwd
    return tmp_path


class TestFanOut:
    def test_mirrors_actionable_to_each_non_admin_user(self, fanout_env):
        signals = [_make_signal("mkt-1"), _make_signal("mkt-2")]
        weather_bot.fan_out_to_users(signals, funnel={"fetched": 10})

        for uid in (USER_A, USER_B):
            csv_path = fanout_env / "logs" / "users" / uid / "paper_trades.csv"
            assert csv_path.exists(), f"user {uid} got no trade log"
            with csv_path.open() as f:
                rows = list(csv.DictReader(f))
            assert len(rows) == 2
            assert {r["market_id"] for r in rows} == {"mkt-1", "mkt-2"}

    def test_admin_is_excluded_from_fanout(self, fanout_env):
        weather_bot.fan_out_to_users([_make_signal()], funnel=None)
        admin_dir = fanout_env / "logs" / "users" / ADMIN_ID
        assert not admin_dir.exists(), "admin must stay on root logs, not get a mirror"

    def test_per_user_signals_file_includes_funnel(self, fanout_env):
        funnel = {"fetched": 100, "parsed": 80, "actionable": 1, "rejections": {"gate4": 5}}
        weather_bot.fan_out_to_users([_make_signal()], funnel=funnel)
        data = json.loads(
            (fanout_env / "logs" / "users" / USER_A / "last_signals.json").read_text()
        )
        assert data["funnel"] == funnel
        assert len(data["signals"]) == 1

    def test_rerun_does_not_duplicate_trades(self, fanout_env):
        signals = [_make_signal("mkt-1")]
        weather_bot.fan_out_to_users(signals, funnel=None)
        weather_bot.fan_out_to_users(signals, funnel=None)
        csv_path = fanout_env / "logs" / "users" / USER_A / "paper_trades.csv"
        with csv_path.open() as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 1, "log_trade idempotency must hold across fan-out runs"

    def test_one_user_failure_does_not_block_others(self, fanout_env, monkeypatch):
        calls = []
        original = PaperTrader.log_trade

        def flaky(self, signal):
            uid = self.log_path.parent.name
            calls.append(uid)
            if uid == USER_A:
                raise OSError("disk full")
            return original(self, signal)

        monkeypatch.setattr(PaperTrader, "log_trade", flaky)
        weather_bot.fan_out_to_users([_make_signal()], funnel=None)

        csv_b = fanout_env / "logs" / "users" / USER_B / "paper_trades.csv"
        assert csv_b.exists(), "user B must be mirrored even when user A fails"
        assert USER_A in calls and USER_B in calls


class TestFanOutResolve:
    def test_per_user_resolve_passes_model_none(self, fanout_env, monkeypatch):
        """Regression guard: per-user resolves must never feed calibration."""
        # Give both users an outstanding trade log
        weather_bot.fan_out_to_users([_make_signal()], funnel=None)

        seen_models = []

        def fake_auto_resolve(self, client, model=None):
            seen_models.append(model)
            return (0, 1)

        monkeypatch.setattr(PaperTrader, "auto_resolve", fake_auto_resolve)
        weather_bot.fan_out_auto_resolve(client=object())

        assert len(seen_models) == 2
        assert all(m is None for m in seen_models), (
            "per-user auto_resolve must pass model=None — anything else "
            "multi-counts outcomes into calibration_log.csv"
        )

    def test_resolve_skips_users_without_trades(self, fanout_env, monkeypatch):
        called = []
        monkeypatch.setattr(
            PaperTrader, "auto_resolve",
            lambda self, client, model=None: called.append(self.log_path) or (0, 0),
        )
        weather_bot.fan_out_auto_resolve(client=object())
        assert called == [], "no per-user CSVs exist yet → no resolve calls"


class TestRegisteredUsers:
    def test_admin_filtered_and_ids_are_ints(self, fanout_env):
        users = weather_bot._load_registered_users()
        assert set(users.keys()) == {int(USER_A), int(USER_B)}

    def test_missing_users_file_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(weather_bot, "USERS_FILE", tmp_path / "nope.json")
        assert weather_bot._load_registered_users() == {}

    def test_corrupt_users_file_returns_empty(self, tmp_path, monkeypatch):
        bad = tmp_path / "users.json"
        bad.write_text("{not json")
        monkeypatch.setattr(weather_bot, "USERS_FILE", bad)
        assert weather_bot._load_registered_users() == {}


class TestLiveFanOut:
    def _users(self, fanout_env, mode="live", confirmed=True):
        users_file = fanout_env / "config" / "users.json"
        users_file.write_text(json.dumps({
            ADMIN_ID: {"role": "admin", "mode": "paper"},
            USER_A: {
                "role": "viewer", "mode": mode,
                **({"live_confirmed_at": "2026-06-12T00:00:00"} if confirmed else {}),
            },
        }))

    def test_live_branch_requires_mode_and_confirmation(self, fanout_env, monkeypatch):
        executed = []
        monkeypatch.setattr(
            weather_bot, "_execute_live_for_user",
            lambda uid, *a, **k: executed.append(uid),
        )
        # paper mode → no live
        self._users(fanout_env, mode="paper")
        weather_bot.fan_out_to_users([_make_signal()], funnel=None)
        assert executed == []
        # live but unconfirmed → no live
        self._users(fanout_env, mode="live", confirmed=False)
        weather_bot.fan_out_to_users([_make_signal("mkt-2")], funnel=None)
        assert executed == []
        # live + confirmed → executes
        self._users(fanout_env, mode="live", confirmed=True)
        weather_bot.fan_out_to_users([_make_signal("mkt-3")], funnel=None)
        assert executed == [int(USER_A)]

    def test_missing_key_writes_halt_and_skips(self, fanout_env, monkeypatch):
        monkeypatch.setattr("weather.secrets.get_user_key", lambda uid: None)
        user_dir = fanout_env / "logs" / "users" / USER_A
        user_dir.mkdir(parents=True)
        weather_bot._execute_live_for_user(
            int(USER_A), {"mode": "live"}, user_dir, object(), [_make_signal()],
        )
        halt = json.loads((user_dir / "live_halt.json").read_text())
        assert "key unavailable" in halt["reason"]

    def test_kill_switch_halts_user_but_not_loop(self, fanout_env, monkeypatch):
        from weather.live_trader import LiveTrader

        monkeypatch.setattr("weather.secrets.get_user_key", lambda uid: "0xkey")
        monkeypatch.setattr(LiveTrader, "fetch_balance", lambda self: 500.0)

        calls = []

        def explode(self, signal):
            calls.append(signal.market.market_id)
            raise RuntimeError("Daily loss limit hit")

        monkeypatch.setattr(LiveTrader, "execute_signal", explode)
        user_dir = fanout_env / "logs" / "users" / USER_A
        user_dir.mkdir(parents=True)
        signals = [_make_signal("mkt-1"), _make_signal("mkt-2")]
        # Must not raise — the halt is contained to this user
        weather_bot._execute_live_for_user(
            int(USER_A), {"mode": "live"}, user_dir, object(), signals,
        )
        assert calls == ["mkt-1"], "halt must stop after the first RuntimeError"
        halt = json.loads((user_dir / "live_halt.json").read_text())
        assert "Daily loss limit" in halt["reason"]

    def test_ledger_caps_bankroll(self, fanout_env, monkeypatch):
        from weather.live_trader import LiveTrader

        monkeypatch.setattr("weather.secrets.get_user_key", lambda uid: "0xkey")
        monkeypatch.setattr(LiveTrader, "fetch_balance", lambda self: 500.0)
        seen = {}
        monkeypatch.setattr(
            LiveTrader, "execute_signal",
            lambda self, s: seen.setdefault("bankroll", self.bankroll_usd),
        )
        user_dir = fanout_env / "logs" / "users" / USER_A
        user_dir.mkdir(parents=True)
        (user_dir / "wallet.json").write_text(json.dumps({
            "transactions": [{"type": "deposit", "amount": 200.0}],
        }))
        weather_bot._execute_live_for_user(
            int(USER_A), {"mode": "live"}, user_dir, object(), [_make_signal()],
        )
        assert seen["bankroll"] == 200.0, "ledger net deposits must cap the bankroll"
