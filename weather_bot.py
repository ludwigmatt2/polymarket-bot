#!/usr/bin/env python3
"""
Weather Bot — Ensemble forecast vs. Polymarket weather market arbitrage.

Modes:
  scan     One-shot: scan markets, evaluate signals, print summary (no logging)
  paper    Continuous paper trading — scan + log signals (default)
  live     Live trading — Kelly-sized orders via the official CLOB SDK (requires .env credentials)
  stats    Print paper trading dashboard and go-live gate status
  resolve  Interactively resolve outstanding paper trades with actual outcomes
  debug    Print full model internals: 7-day ensemble, scaling ratio, projected totals

Run:
  python weather_bot.py --mode scan
  python weather_bot.py --mode paper --interval 3600
  python weather_bot.py --mode live --bankroll 500 --interval 3600
  python weather_bot.py --mode stats
  python weather_bot.py --mode debug
  python weather_bot.py --mode debug --city Seoul
"""

import argparse
import calendar
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from weather.backtest import (
    Backtester, BacktestResult, default_test_suite, summarize,
    TempBacktester, TempBacktestResult, default_temp_test_suite, summarize_temp,
)
from weather.live_backtest import LiveSignalBacktester
from weather.market_scanner import WeatherMarketScanner
from weather.city_bias import CityBiasCorrector
from weather.paths import DATA_DIR
from weather.models import Signal
from weather.live_trader import LiveTrader, check_geoblock
from weather.paper_trader import PaperTrader
from weather.position_monitor import PositionMonitor, print_divergences, print_flags
from weather.price_tracker import PriceTracker
from weather.probability_model import ProbabilityModel
from weather.signal_generator import SignalGenerator
from weather.weather_client import WeatherClient

# All logs live under the persistent data dir (RAILWAY_VOLUME_MOUNT_PATH on the VPS,
# repo root locally). Keep every writer here in sync with telegram_bot.py's readers,
# which resolve the same DATA_DIR / "logs" — otherwise scans write one place and the
# bot reads another (the cause of silent notifications + stale /status).
DEFAULT_LOG_DIR = DATA_DIR / "logs"


def run_scan(
    scanner: WeatherMarketScanner,
    generator: SignalGenerator,
    paper: PaperTrader | None,
    log_dir: Path = DEFAULT_LOG_DIR,
    monitor: PositionMonitor | None = None,
    live_trader: "LiveTrader | None" = None,
    all_users: bool = False,
) -> list[Signal]:
    """Single scan cycle: find markets → evaluate signals → optionally log or execute."""
    print("  [1/3] Scanning Polymarket for weather markets...", end=" ", flush=True)
    markets = scanner.scan()
    print(f"{len(markets)} tradeable found")

    if not markets:
        print("  No tradeable weather markets found. Check logs/unparseable_markets.csv")
        return []

    print(f"  [2/3] Evaluating {len(markets)} markets...", end=" ", flush=True)
    signals = [generator.evaluate(m) for m in markets]
    actionable = [s for s in signals if s.quality_gate_passed]
    rejected = [s for s in signals if not s.quality_gate_passed]
    print(f"{len(actionable)} signals pass quality gates")

    geo = check_geoblock() if (live_trader and actionable) else None
    if live_trader and actionable and geo and geo.get("blocked"):
        print(f"  LIVE HALT: geoblocked from {geo.get('country','?')}/{geo.get('region','?')} "
              f"(IP {geo.get('ip','?')}) — route order traffic through a permitted region "
              f"(set HTTPS_PROXY).", file=sys.stderr)
    elif live_trader and actionable:
        print(f"  [3/3] Executing {len(actionable)} live order(s)...")
        for s in actionable:
            try:
                result = live_trader.execute_signal(s)
                if result and result.get("filled", 0) > 0:
                    print(f"    ✓ {s.market.title[:55]} | {s.direction} ${result['size_usd']:.2f} @ {result['price']:.3f}  order={result['order_id'][:10]}")
                elif result:
                    print(f"    – {s.market.title[:55]} | unfilled (no depth within slippage cap)")
                else:
                    print(f"    – {s.market.title[:55]} | skipped (size too small)")
            except RuntimeError as e:
                print(f"  LIVE HALT: {e}", file=sys.stderr)
                raise
            except Exception as e:
                print(f"    ✗ {s.market.title[:55]} | order failed: {e}", file=sys.stderr)
    elif paper and actionable:
        print(f"  [3/3] Logging {len(actionable)} paper trades...", end=" ", flush=True)
        logged = [paper.log_trade(s) for s in actionable]
        print(f"{sum(1 for t in logged if t)} logged")

    _print_scan_summary(signals, actionable, rejected)
    funnel = _build_funnel(scanner, signals, actionable, rejected)
    _write_signals_file(actionable, log_dir, funnel)

    if all_users:
        fan_out_to_users(actionable, funnel)

    if monitor is not None:
        print("  [4/4] Checking open positions (mark-to-model)...", end=" ", flush=True)
        flags = monitor.check_open_positions()
        print(f"{len(flags)} flag(s)")
        if flags:
            print()
            print_flags(flags)

    if live_trader is not None:
        print("  [+] Reconciling open positions on-chain...")
        try:
            divergences = live_trader.reconcile_positions()
        except Exception as e:
            print(f"  reconcile skipped ({e})", file=sys.stderr)
        else:
            print_divergences(divergences)

    return signals


USERS_FILE = DATA_DIR / "config" / "users.json"


def _load_registered_users() -> dict[int, dict]:
    """Read users.json directly (no telegram import — runs as a subprocess/launchd)."""
    import json
    import os
    if not USERS_FILE.exists():
        return {}
    try:
        users = {int(k): v for k, v in json.loads(USERS_FILE.read_text()).items()}
    except (ValueError, json.JSONDecodeError):
        return {}
    # The admin IS the root log — fan-out only mirrors to everyone else.
    admin_id = os.environ.get("TELEGRAM_ADMIN_ID", "")
    return {uid: u for uid, u in users.items() if str(uid) != admin_id}


def fan_out_to_users(actionable: list[Signal], funnel: dict | None = None) -> None:
    """
    Mirror the already-computed signals to each registered user's log dir, and
    execute live orders for users in live mode.

    One model, separate money: the scan/model ran once at root; this only applies
    per-user context (their CSV, their signals file, their wallet for live).
    Never touches calibration. One user's failure never blocks the others.
    """
    users = _load_registered_users()
    if not users:
        return
    print(f"  Fan-out: mirroring {len(actionable)} signal(s) to {len(users)} user(s)...")
    root_paper = PaperTrader(log_path=DEFAULT_LOG_DIR / "paper_trades.csv")  # global gate: one model, one track record
    for uid, user in users.items():
        user_dir = DEFAULT_LOG_DIR / "users" / str(uid)
        try:
            user_dir.mkdir(parents=True, exist_ok=True)
            user_paper = PaperTrader(log_path=user_dir / "paper_trades.csv")
            for s in actionable:
                user_paper.log_trade(s)
            _write_signals_file(actionable, user_dir, funnel)
        except Exception as e:
            print(f"    ✗ fan-out failed for user {uid}: {e}", file=sys.stderr)
            continue

        if user.get("mode") == "live" and user.get("live_confirmed_at") and actionable:
            _execute_live_for_user(uid, user, user_dir, root_paper, actionable)


def _write_live_halt(user_dir: Path, reason: str) -> None:
    """Persist a per-user live halt for the Telegram bot's check_alerts to surface."""
    import json
    (user_dir / "live_halt.json").write_text(json.dumps({
        "halted_at": datetime.utcnow().isoformat(),
        "reason": reason,
    }))


def _user_ledger_cap(user_dir: Path) -> float | None:
    """Net deposits from the user's manual ledger, as a bankroll cap (None = no ledger)."""
    import json
    wallet_file = user_dir / "wallet.json"
    if not wallet_file.exists():
        return None
    try:
        txns = json.loads(wallet_file.read_text()).get("transactions", [])
    except (json.JSONDecodeError, OSError):
        return None
    net = sum(t["amount"] for t in txns if t.get("type") == "deposit") \
        - sum(t["amount"] for t in txns if t.get("type") == "withdraw")
    return net if net > 0 else None


def _execute_live_for_user(
    uid: int, user: dict, user_dir: Path, root_paper: PaperTrader, actionable: list[Signal],
) -> None:
    """Execute the shared signals with this user's wallet. Failures stay per-user."""
    from weather.secrets import get_user_creds

    creds = get_user_creds(uid)
    if not creds or not creds.get("pk"):
        _write_live_halt(user_dir, "private key unavailable — skipped live execution")
        print(f"    ⚠ user {uid}: no key available, live skipped", file=sys.stderr)
        return

    geo = check_geoblock()
    if geo and geo.get("blocked"):
        reason = (f"geoblocked — order placement restricted from "
                  f"{geo.get('country','?')}/{geo.get('region','?')} (IP {geo.get('ip','?')}); "
                  f"set HTTPS_PROXY to a permitted region")
        _write_live_halt(user_dir, reason)
        print(f"    ⛔ user {uid}: {reason}", file=sys.stderr)
        return

    trader = LiveTrader.from_creds(
        creds,
        paper_trader=root_paper,  # global gate; per-user paper already mirrored
        log_path=user_dir / "live_trades.csv",
        idempotency_path=user_dir / "live_idempotency.json",
    )
    try:
        balance = trader.fetch_balance()
    except Exception as e:
        _write_live_halt(user_dir, f"balance fetch failed: {e}")
        print(f"    ⚠ user {uid}: balance fetch failed ({e}), live skipped", file=sys.stderr)
        return
    if balance < 1.0:
        print(f"    – user {uid}: balance ${balance:.2f} too small, live skipped")
        return
    ledger_cap = _user_ledger_cap(user_dir)
    trader.bankroll_usd = min(balance, ledger_cap) if ledger_cap else balance

    print(f"    user {uid}: LIVE, bankroll=${trader.bankroll_usd:.2f}, "
          f"{len(actionable)} signal(s)")
    for s in actionable:
        try:
            result = trader.execute_signal(s)
            if result and result.get("filled", 0) > 0:
                print(f"      ✓ {s.market.title[:50]} | {s.direction} "
                      f"${result['size_usd']:.2f} order={result['order_id'][:10]}")
        except RuntimeError as e:
            # Hard halt (kill switch / gates) — stop THIS user, never the others.
            _write_live_halt(user_dir, str(e))
            print(f"    ⛔ user {uid} live halt: {e}", file=sys.stderr)
            return
        except Exception as e:
            print(f"      ✗ user {uid}: order failed: {e}", file=sys.stderr)


def fan_out_auto_resolve(client: WeatherClient) -> None:
    """
    Resolve every registered user's paper AND live trades. model=None is
    mandatory: outcomes feed calibration_log.csv exactly once, via the root resolve.
    """
    users = _load_registered_users()
    for uid in users:
        user_dir = DEFAULT_LOG_DIR / "users" / str(uid)
        trades_csv = user_dir / "paper_trades.csv"
        if trades_csv.exists():
            try:
                user_paper = PaperTrader(log_path=trades_csv)
                resolved, skipped = user_paper.auto_resolve(client, model=None)
                if resolved:
                    _write_resolved_file(user_paper, resolved, user_dir)
                print(f"  User {uid}: auto-resolved {resolved} trade(s).  {skipped} skipped.")
            except Exception as e:
                print(f"    ✗ resolve failed for user {uid}: {e}", file=sys.stderr)

        live_csv = user_dir / "live_trades.csv"
        if live_csv.exists():
            try:
                from weather.secrets import get_user_creds
                user_live = LiveTrader.from_creds(
                    get_user_creds(uid),
                    paper_trader=PaperTrader(log_path=trades_csv),
                    log_path=live_csv,
                    idempotency_path=user_dir / "live_idempotency.json",
                )
                live_resolved, live_skipped = user_live.auto_resolve(client, model=None)
                print(f"  User {uid} live: auto-resolved {live_resolved} trade(s).  "
                      f"{live_skipped} skipped.")
                _claim = user_live.claim_winnings()
                if _claim.get("claimed"):
                    print(f"  User {uid} live: claimed {_claim['claimed']} resolved position(s) → pUSD.")
                elif _claim.get("error"):
                    print(f"  User {uid} live: claim skipped ({_claim['error']})", file=sys.stderr)
            except Exception as e:
                print(f"    ✗ live resolve failed for user {uid}: {e}", file=sys.stderr)


def _build_funnel(
    scanner: WeatherMarketScanner,
    signals: list[Signal],
    actionable: list[Signal],
    rejected: list[Signal],
) -> dict:
    """Scan funnel for /scanreport: fetched → parsed → evaluated → gates → actionable."""
    rejections: dict[str, int] = {}
    for s in rejected:
        key = s.rejection_reason.split(":")[0] if s.rejection_reason else "unknown"
        rejections[key] = rejections.get(key, 0) + 1
    top_rejected = [
        {
            "title": s.market.title,
            "reason": s.rejection_reason or "unknown",
            "edge_pp": s.edge_pp,
        }
        for s in sorted(rejected, key=lambda x: abs(x.edge_pp), reverse=True)[:10]
    ]
    return {
        **scanner.last_funnel,
        "evaluated": len(signals),
        "actionable": len(actionable),
        "rejections": dict(sorted(rejections.items(), key=lambda x: -x[1])),
        "top_rejected": top_rejected,
    }


def _write_signals_file(
    actionable: list[Signal], log_dir: Path = DEFAULT_LOG_DIR, funnel: dict | None = None,
) -> None:
    import json
    log_dir.mkdir(parents=True, exist_ok=True)
    out = log_dir / "last_signals.json"
    payload = {
        "scanned_at": datetime.utcnow().isoformat(),
        "signals": [
            {
                "title": s.market.title,
                "edge_pp": s.edge_pp,
                "model_p": s.model_p,
                "mkt_p": s.market_p,
                "direction": s.direction,
                "resolution_date": s.market.resolution_date.isoformat() if s.market.resolution_date else None,
            }
            for s in sorted(actionable, key=lambda x: x.edge_pp, reverse=True)
        ],
    }
    if funnel:
        payload["funnel"] = funnel
    out.write_text(json.dumps(payload))


def _write_resolved_file(paper: "PaperTrader", resolved_count: int, log_dir: Path = DEFAULT_LOG_DIR) -> None:
    import csv as _csv
    import json
    log_dir.mkdir(parents=True, exist_ok=True)
    trades_path = log_dir / "paper_trades.csv"
    if not trades_path.exists():
        return
    with trades_path.open() as f:
        rows = list(_csv.DictReader(f))
    # Most recently resolved trades (by resolved_at timestamp)
    recently_resolved = sorted(
        [r for r in rows if r.get("resolved_at")],
        key=lambda x: x.get("resolved_at", ""), reverse=True
    )[:resolved_count]
    (log_dir / "last_resolved.json").write_text(json.dumps({
        "resolved_at": __import__("datetime").datetime.utcnow().isoformat(),
        "count": resolved_count,
        "resolved": [
            {
                "market_title": r.get("market_title", ""),
                "direction": r.get("direction", ""),
                "pnl_usd": float(r.get("pnl_usd", 0)),
                "resolved_at": r.get("resolved_at", ""),
            }
            for r in recently_resolved
        ],
    }))


def _print_scan_summary(signals: list[Signal], actionable: list[Signal], rejected: list[Signal]) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"\n[{ts}] {len(signals)} evaluated  |  {len(actionable)} actionable  |  {len(rejected)} rejected")

    if actionable:
        print(f"\n  {'Edge':>6}  {'ModelP':>7}  {'MktP':>6}  {'Dir':>4}  {'Spread':>7}  Market")
        print(f"  {'-'*6}  {'-'*7}  {'-'*6}  {'-'*4}  {'-'*7}  {'-'*45}")
        for s in sorted(actionable, key=lambda x: x.edge_pp, reverse=True):
            print(f"  {s.edge_pp:>6.1%}  {s.model_p:>7.3f}  {s.market_p:>6.3f}  "
                  f"{s.direction:>4}  {s.ensemble_spread:>7.3f}  {s.market.title[:45]}")

    if rejected:
        reasons: dict[str, int] = {}
        for s in rejected:
            key = s.rejection_reason.split(":")[0] if s.rejection_reason else "unknown"
            reasons[key] = reasons.get(key, 0) + 1
        print(f"\n  Rejection reasons: {dict(sorted(reasons.items(), key=lambda x: -x[1]))}")


def mode_debug(scanner: WeatherMarketScanner, generator: SignalGenerator, city_filter: str | None) -> None:
    """
    Print full model internals for each tradeable market:
    ensemble member arrays, scaling ratio (monthly only), per-model probabilities,
    and the resulting Signal — all derived from the forecast already attached
    to the Signal, so no extra API calls are made.
    """
    print("  Scanning markets...", end=" ", flush=True)
    markets = scanner.scan()
    print(f"{len(markets)} tradeable\n")

    for market in markets:
        if city_filter and city_filter.lower() not in market.title.lower():
            continue

        sig = generator.evaluate(market)
        unit = WeatherClient.METRIC_UNITS.get(market.metric, "")
        loc = market.location
        forecast = sig.forecast

        print("─" * 70)
        print(f"  {market.title}")
        print(f"  Market ID : {market.market_id}")
        threshold_str = f"{market.threshold}{unit}"
        if market.threshold_high is not None:
            threshold_str += f"–{market.threshold_high}{unit}"
        print(f"  Metric    : {market.metric}  |  Threshold: {threshold_str}  Direction: {market.direction}")
        print(f"  Market P  : {market.yes_price:.3f} YES  |  Liquidity: ${market.liquidity_usd:.0f}")
        print(f"  Location  : {loc.city} ({loc.lat:.2f}, {loc.lon:.2f})")

        members = forecast.all_members
        if members:
            label = "Projected" if forecast.scaling_ratio is not None else "Forecast"
            mean = sum(members) / len(members)
            print(f"  Members   : {len(members)} total")
            if forecast.scaling_ratio is not None:
                print(f"  Scaling   : {forecast.scaling_ratio:.3f}x  (7-day ensemble × ratio = full-month total)")
            print(f"  {label} : mean={mean:.1f}{unit}  min={min(members):.1f}{unit}  max={max(members):.1f}{unit}")
            for mdl, vals in forecast.member_arrays.items():
                if not vals:
                    continue
                m_mean = sum(vals) / len(vals)
                m_p = sig.prob_result.model_breakdown.get(mdl)
                p_str = f"  P={m_p:.3f}" if m_p is not None else ""
                print(f"    {mdl:<20}  n={len(vals)}  range=[{min(vals):.1f}–{max(vals):.1f}{unit}  avg={m_mean:.1f}{unit}]{p_str}")
        else:
            print("  Members   : 0 (forecast fetch failed)")

        gate = "PASS" if sig.quality_gate_passed else f"FAIL ({sig.rejection_reason})"
        print(f"  Model P   : {sig.model_p:.4f}   Edge: {sig.edge_pp:.1%}   Dir: {sig.direction}   "
              f"Spread: {sig.ensemble_spread:.4f}   Gate: {gate}")
        print()


def mode_arb(*_args, **_kwargs) -> None:
    """Arb mode was removed — the pmxt.Router() dependency has been retired."""
    print(
        "  ❌ Arb mode is no longer supported.\n"
        "  The pmxt library has been replaced by the official py-clob-client-v2 SDK,\n"
        "  which does not include cross-venue arbitrage scanning."
    )


def mode_backtest(client: WeatherClient) -> None:
    """
    Replay the model against historical months and score it against a
    climatology baseline. Tests whether the current monthly-aggregate
    approach has any real skill.
    """
    cases = default_test_suite()
    print(f"  Running {len(cases)} backtest cases (4 cities × ~3 years × 4 months)...")
    print(f"  Threshold per case: train-year median (climatology forced to ~0.5)\n")

    bt = Backtester(client)
    results = bt.run_suite(cases)

    if not results:
        print("  No results — archive fetches failed.")
        return

    print(f"  {'City':<12} {'Y/M':<8} {'thr':>5} {'actual':>7}  {'P_clim':>6} {'P_old':>6} {'P_bld':>6}  {'B_clim':>6} {'B_old':>6} {'B_bld':>6} out")
    print(f"  {'-'*12} {'-'*8} {'-'*5} {'-'*7}  {'-'*6} {'-'*6} {'-'*6}  {'-'*6} {'-'*6} {'-'*6} ---")
    for r in results:
        c = r.case
        print(f"  {c.city:<12} {c.test_year}/{c.month:02d}  "
              f"{r.threshold_used:>5.0f} {r.actual_total:>7.1f}  "
              f"{r.p_climatology:>6.3f} {r.p_model_old:>6.3f} {r.p_model_blend:>6.3f}  "
              f"{r.brier_climatology:>6.3f} {r.brier_model_old:>6.3f} {r.brier_model_blend:>6.3f} {r.actual_binary}")

    s = summarize(results)
    print()
    print("  ═══════════════════════════════════════════════════════════════════")
    print(f"  N = {s['n']} cases")
    print(f"  Mean Brier — Climatology       : {s['mean_brier_climatology']:.4f}")
    print(f"  Mean Brier — Model (old ratio) : {s['mean_brier_model_old']:.4f}    BSS={s['bss_old']:+.4f}  "
          f"{'✓' if s['old_beats_clim'] else '✗'}")
    print(f"  Mean Brier — Model (blend)     : {s['mean_brier_model_blend']:.4f}    BSS={s['bss_blend']:+.4f}  "
          f"{'✓' if s['blend_beats_clim'] else '✗'}")
    print("  ═══════════════════════════════════════════════════════════════════")

    _print_breakdowns(results)
    _save_results_csv(results, Path("logs/backtest_results.csv"))


def mode_backtest_temp(client: WeatherClient) -> None:
    """
    Replay the climatological temperature model against historical daily actuals.
    Tests whether same-day-of-year historical distributions have skill over a
    coin-flip baseline — the minimum bar any live model must clear.

    144 cases: 4 cities × 12 months × 3 test years (2022-2024).
    Uses ±7-day window from prior years as the climatological distribution.
    Takes ~2-3 min (archive API calls, cached within session).
    """
    cases = default_temp_test_suite()
    print(f"  Running {len(cases)} temperature backtest cases "
          f"(4 cities × 12 months × 3 years)...")
    print(f"  Threshold per case: train-year median (balanced test)\n")

    bt = TempBacktester(client)
    results = bt.run_suite(cases)

    if not results:
        print("  No results — archive fetches failed.")
        return

    # Per-case table
    print(f"  {'City':<15} {'Date':<12} {'thr':>5} {'actual':>7}  "
          f"{'P_clim':>6}  {'B_clim':>6}  out")
    print(f"  {'-'*15} {'-'*12} {'-'*5} {'-'*7}  {'-'*6}  {'-'*6}  ---")
    for r in results:
        print(f"  {r.case.city:<15} {r.case.target_date.isoformat():<12} "
              f"{r.threshold_used:>5.1f} {r.actual_value:>7.1f}  "
              f"{r.p_climatology:>6.3f}  {r.brier_climatology:>6.3f}  {r.actual_binary}")

    s = summarize_temp(results)
    print()
    print("  ══════════════════════════════════════════════════════")
    print(f"  N = {s['n']} cases")
    print(f"  Mean Brier (climatology) : {s['mean_brier']:.4f}")
    print(f"  Baseline (coin-flip)     : 0.2500")
    print(f"  BSS vs coin-flip         : {s['bss_vs_coinflip']:+.4f}  "
          f"{'✓ beats random' if s['beats_coinflip'] else '✗ worse than random'}")
    print("  ══════════════════════════════════════════════════════")

    _print_temp_breakdowns(results)
    _save_temp_results_csv(results, Path("logs/backtest_temp_results.csv"))


def _print_temp_breakdowns(results: list[TempBacktestResult]) -> None:
    def _row(label: str, rs: list[TempBacktestResult]) -> str:
        s = summarize_temp(rs)
        flag = "✓" if s["beats_coinflip"] else "✗"
        return (f"    {label:<15}  n={s['n']:<3}  "
                f"brier={s['mean_brier']:.3f}  BSS={s['bss_vs_coinflip']:+.3f} {flag}")

    print("\n  Per-city:")
    by_city: dict[str, list] = {}
    for r in results:
        by_city.setdefault(r.case.city, []).append(r)
    for city, rs in by_city.items():
        print(_row(city, rs))

    print("\n  Per-month:")
    by_month: dict[int, list] = {}
    for r in results:
        by_month.setdefault(r.case.target_date.month, []).append(r)
    for m in sorted(by_month):
        print(_row(f"month={calendar.month_abbr[m]}", by_month[m]))


def _save_temp_results_csv(results: list[TempBacktestResult], path: Path) -> None:
    import csv
    path.parent.mkdir(exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["city", "metric", "target_date", "threshold", "actual_value",
                    "actual_binary", "p_climatology", "train_n", "brier_climatology"])
        for r in results:
            w.writerow([
                r.case.city, r.case.metric, r.case.target_date.isoformat(),
                round(r.threshold_used, 2), round(r.actual_value, 2),
                r.actual_binary, round(r.p_climatology, 4),
                r.train_n, round(r.brier_climatology, 6),
            ])
    print(f"\n  Results saved → {path}")


def _print_breakdowns(results: list[BacktestResult]) -> None:
    def _row(label: str, s: dict) -> str:
        old_flag = "✓" if s["old_beats_clim"] else "✗"
        bld_flag = "✓" if s["blend_beats_clim"] else "✗"
        return (f"    {label:<14}  n={s['n']:<3}  clim={s['mean_brier_climatology']:.3f}  "
                f"old={s['mean_brier_model_old']:.3f} {old_flag} BSS={s['bss_old']:+.3f}  "
                f"blend={s['mean_brier_model_blend']:.3f} {bld_flag} BSS={s['bss_blend']:+.3f}")

    print("\n  Per-city Brier Skill Score:")
    by_city: dict[str, list[BacktestResult]] = {}
    for r in results:
        by_city.setdefault(r.case.city, []).append(r)
    for city, rs in by_city.items():
        print(_row(city, summarize(rs)))

    print("\n  Per-month Brier Skill Score:")
    by_month: dict[int, list[BacktestResult]] = {}
    for r in results:
        by_month.setdefault(r.case.month, []).append(r)
    for m in sorted(by_month):
        print(_row(f"month={m}", summarize(by_month[m])))


def _save_results_csv(results: list[BacktestResult], path: Path) -> None:
    import csv
    path.parent.mkdir(exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["city", "metric", "year", "month", "threshold", "actual_total",
                    "actual_binary", "model_pred", "sigma",
                    "p_climatology", "p_model_old", "p_model_blend",
                    "brier_climatology", "brier_model_old", "brier_model_blend", "train_n"])
        for r in results:
            w.writerow([r.case.city, r.case.metric, r.case.test_year, r.case.month,
                        r.threshold_used, r.actual_total, r.actual_binary,
                        r.model_point_pred, r.sigma,
                        r.p_climatology, r.p_model_old, r.p_model_blend,
                        r.brier_climatology, r.brier_model_old, r.brier_model_blend, r.train_n])
    print(f"\n  Full results saved → {path}")


def mode_backtest_live(client: WeatherClient, model: ProbabilityModel, log_dir: Path) -> None:
    """
    Replay all resolved paper trades through the live signal path.

    For each resolved trade in logs/paper_trades.csv, reconstructs the market,
    builds a synthetic ensemble from archive historical data, then runs the full
    SignalGenerator (gates, calibration, shrinkage). Reports:
      - Parity %: fraction of replays whose direction matches the original signal.
      - Brier delta: mean(replayed Brier) − mean(original Brier).
    """
    trades_csv = log_dir / "paper_trades.csv"
    backtester = LiveSignalBacktester(model=model, client=client)
    print(f"  Replaying resolved trades from {trades_csv} ...")
    report = backtester.replay_trades(trades_csv)

    print()
    print("  ══════════════ Live Signal Backtest ══════════════")
    print(f"  Resolved trades loaded : {report.n_total}")
    print(f"  Successfully replayed  : {report.n_replayed}  "
          f"({'archive fetch failed' if report.n_replayed < report.n_total else 'all'})")
    if report.n_replayed == 0:
        print("  No trades replayed — archive data unavailable for these dates.")
        return
    print(f"  Direction parity       : {report.n_direction_match}/{report.n_replayed}  "
          f"({report.parity_pct:.1%})")
    print(f"  Mean Brier (original)  : {report.mean_brier_original:.4f}")
    print(f"  Mean Brier (replayed)  : {report.mean_brier_replayed:.4f}")
    delta_sign = "+" if report.mean_brier_delta > 0 else ""
    print(f"  Brier delta            : {delta_sign}{report.mean_brier_delta:.4f}  "
          f"({'replayed worse' if report.mean_brier_delta > 0 else 'replayed better or equal'})")
    print()

    mismatches = [r for r in report.results if not r.direction_match]
    if mismatches:
        print(f"  Direction mismatches ({len(mismatches)}):")
        print(f"  {'ID':>8}  {'Orig':>4}  {'Replay':>6}  {'Brier-O':>7}  {'Brier-R':>7}  Title")
        print(f"  {'-'*8}  {'-'*4}  {'-'*6}  {'-'*7}  {'-'*7}  {'-'*45}")
        for r in sorted(mismatches, key=lambda x: abs(x.brier_replayed - x.brier_original), reverse=True)[:10]:
            print(f"  {r.trade_id[:8]:>8}  {r.original_direction:>4}  {r.replayed_direction:>6}  "
                  f"{r.brier_original:>7.4f}  {r.brier_replayed:>7.4f}  {r.market_title[:45]}")
    print("  ══════════════════════════════════════════════════")


def mode_stats(paper: PaperTrader) -> None:
    paper.print_dashboard()


def mode_resolve(paper: PaperTrader) -> None:
    """Interactive resolution of outstanding paper trades."""
    trades = paper._load_all()
    outstanding = [t for t in trades if t.get("actual_outcome") in (None, "", "None")]
    if not outstanding:
        print("No outstanding paper trades to resolve.")
        return
    print(f"\n{len(outstanding)} outstanding trades:\n")
    for t in outstanding:
        print(f"  [{t['trade_id']}] {t['market_title'][:60]}")
        print(f"    Entry: {t['entry_price']}  Direction: {t['direction']}  "
              f"Resolution: {t['resolution_date']}")
        ans = input("    Outcome? [y=YES resolved / n=NO resolved / s=skip]: ").strip().lower()
        if ans == "y":
            paper.resolve_trade(t["trade_id"], actual_outcome=True)
            print("    → Resolved YES")
        elif ans == "n":
            paper.resolve_trade(t["trade_id"], actual_outcome=False)
            print("    → Resolved NO")
        else:
            print("    → Skipped")


def main() -> None:
    parser = argparse.ArgumentParser(description="Weather model arbitrage bot")
    parser.add_argument("--mode", choices=["scan", "paper", "live", "stats", "resolve", "auto-resolve", "backfill-calibration", "debug", "backtest", "backtest-temp", "backtest-live", "arb"],
                        default="paper", help="Operating mode (default: paper)")
    parser.add_argument("--interval", type=int, default=3600,
                        help="Re-scan interval in seconds for paper/live mode (default: 3600)")
    parser.add_argument("--bankroll", type=float, default=500.0,
                        help="Live trading bankroll in USD for Kelly sizing (default: 500)")
    parser.add_argument("--city", type=str, default=None,
                        help="Filter markets by city name (debug mode only)")
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR,
                        help="Directory for trade logs and signal files (default: logs/)")
    parser.add_argument("--all-users", action="store_true",
                        help="After the root scan/resolve, mirror results to every "
                             "registered user's log dir (config/users.json)")
    args = parser.parse_args()

    log_dir: Path = args.log_dir
    log_dir.mkdir(parents=True, exist_ok=True)

    print(f"Weather Bot  [mode={args.mode}  log-dir={log_dir}]")
    print()

    if args.mode == "stats":
        mode_stats(PaperTrader(log_path=log_dir / "paper_trades.csv"))
        return

    if args.mode == "resolve":
        mode_resolve(PaperTrader(log_path=log_dir / "paper_trades.csv"))
        return

    if args.mode == "auto-resolve":
        paper  = PaperTrader(log_path=log_dir / "paper_trades.csv")
        model  = ProbabilityModel(calibration_log_path=log_dir / "calibration_log.csv")
        client = WeatherClient()

        resolved_count, skipped = paper.auto_resolve(client, model=model)
        print(f"  Paper: auto-resolved {resolved_count} trade(s).  {skipped} skipped.")
        if resolved_count:
            paper.print_dashboard()
            _write_resolved_file(paper, resolved_count, log_dir)
            n_obs = model.n_calibration_obs
            active = model._calibrator is not None
            dirs  = list(model._calibrators_by_dir.keys())
            print(f"  Calibration log: {n_obs} obs  |  active={active}  |  per-direction={dirs or 'none yet'}")

        live_log = log_dir / "live_trades.csv"
        if live_log.exists():
            from weather.secrets import get_user_creds
            _admin_uid = int(os.environ.get("TELEGRAM_ADMIN_ID") or os.environ.get("ADMIN_ID") or "0")
            live_trader = LiveTrader.from_creds(
                get_user_creds(_admin_uid) if _admin_uid else None,
                paper_trader=paper, log_path=live_log,
            )
            live_resolved, live_skipped = live_trader.auto_resolve(client, model=model)
            print(f"  Live:  auto-resolved {live_resolved} trade(s).  {live_skipped} skipped.")
            _claim = live_trader.claim_winnings()
            if _claim.get("claimed"):
                print(f"  Live:  claimed {_claim['claimed']} resolved position(s) → pUSD.")
            elif _claim.get("error"):
                print(f"  Live:  claim skipped ({_claim['error']})", file=sys.stderr)

        if args.all_users:
            fan_out_auto_resolve(client)

        return

    if args.mode == "backfill-calibration":
        paper = PaperTrader(log_path=log_dir / "paper_trades.csv")
        model = ProbabilityModel(calibration_log_path=log_dir / "calibration_log.csv")
        resolved = [t for t in paper._load_all() if t.get("actual_outcome") not in (None, "", "None")]
        count = 0
        for t in resolved:
            try:
                model.log_observation(
                    float(t["model_p"]),
                    bool(int(t["actual_outcome"])),
                    direction=t.get("weather_direction", ""),
                )
                count += 1
            except (ValueError, KeyError):
                pass
        n_obs = model.n_calibration_obs
        active = model._calibrator is not None
        dirs   = list(model._calibrators_by_dir.keys())
        print(f"  Backfilled {count} observations into {log_dir / 'calibration_log.csv'}")
        print(f"  Total obs: {n_obs}  |  global calibrator active: {active}  |  per-direction: {dirs or 'none'}")
        return

    if args.mode == "backtest":
        mode_backtest(WeatherClient())
        return

    if args.mode == "backtest-temp":
        mode_backtest_temp(WeatherClient())
        return

    if args.mode == "backtest-live":
        _client = WeatherClient()
        _model = ProbabilityModel(calibration_log_path=log_dir / "calibration_log.csv")
        mode_backtest_live(_client, _model, log_dir)
        return

    if args.mode == "arb":
        mode_arb(min_spread=args.min_spread, limit=args.limit)
        return

    # Build components
    client        = WeatherClient()
    model         = ProbabilityModel(calibration_log_path=log_dir / "calibration_log.csv")
    bias          = CityBiasCorrector()
    scanner       = WeatherMarketScanner()
    price_tracker = PriceTracker()
    generator     = SignalGenerator(model=model, client=client, price_tracker=price_tracker, bias_corrector=bias)
    paper         = PaperTrader(log_path=log_dir / "paper_trades.csv") if args.mode in ("paper", "live") else None
    monitor       = PositionMonitor(client=client, model=model,
                                    trades_csv=log_dir / "paper_trades.csv",
                                    bias_corrector=bias)

    if args.mode == "debug":
        mode_debug(scanner, generator, city_filter=args.city)
        return

    if args.mode == "scan":
        run_scan(scanner, generator, paper=None, log_dir=log_dir, monitor=monitor)
        return

    live_trader = None
    if args.mode == "live":
        from weather.secrets import get_user_creds, set_user_creds
        admin_uid = int(os.environ.get("TELEGRAM_ADMIN_ID") or os.environ.get("ADMIN_ID") or "0")
        _creds = get_user_creds(admin_uid) if admin_uid else None
        # Auto-derive L2 creds if missing (first run after key was stored)
        if _creds and _creds.get("pk") and not _creds.get("clob_api_key"):
            try:
                from weather.secrets import derive_and_store_clob_creds
                l2 = derive_and_store_clob_creds(admin_uid)
                _creds.update(l2)
                print("  ✅ L2 CLOB credentials derived and stored.")
            except Exception as exc:
                print(f"  ⚠️  L2 credential derivation failed: {exc}", file=sys.stderr)
        if not _creds or not _creds.get("pk"):
            # One-time migration: seed from env vars if still present
            pk_env = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
            funder_env = os.environ.get("POLYMARKET_FUNDER_ADDRESS", "") or os.environ.get("POLYMARKET_PROXY_ADDRESS", "")
            if pk_env and admin_uid:
                sig = 1 if funder_env else 0  # POLY_PROXY vs EOA
                set_user_creds(
                    admin_uid, pk=pk_env,
                    funder_address=funder_env or None, signature_type=sig,
                )
                _creds = {"pk": pk_env, "funder_address": funder_env or None, "signature_type": sig}
                print(
                    "  ℹ️  Migrated admin credentials from env to encrypted store.\n"
                    "  You can now remove POLYMARKET_PRIVATE_KEY and POLYMARKET_PROXY_ADDRESS from .env."
                )
                # Derive L2 CLOB credentials so balance checks work
                try:
                    from weather.secrets import derive_and_store_clob_creds
                    l2 = derive_and_store_clob_creds(admin_uid)
                    _creds.update(l2)
                    print("  ✅ L2 CLOB credentials derived and stored.")
                except Exception as exc:
                    print(f"  ⚠️  L2 credential derivation failed: {exc}", file=sys.stderr)
            else:
                print(
                    "  ❌ No admin credentials in encrypted store.\n"
                    "  Run the Telegram bot onboarding (/wallet_setup) to store credentials.",
                    file=sys.stderr,
                )
                sys.exit(1)
        live_trader = LiveTrader.from_creds(
            _creds,
            paper_trader=paper,
            bankroll_usd=args.bankroll,
            log_path=log_dir / "live_trades.csv",
            idempotency_path=log_dir / "live_idempotency.json",
        )
        if not live_trader.is_unlocked():
            print("  Go-live gates not passed. Run: python weather_bot.py --mode stats", file=sys.stderr)
            sys.exit(1)
        balance = live_trader.fetch_balance()
        from weather.config import MAX_LIVE_TRADE_USD, KELLY_FRACTION
        print(f"  Live mode — bankroll=${args.bankroll:.0f}  USDC balance=${balance:.2f}  Kelly={KELLY_FRACTION:.2f}x  max_order=${MAX_LIVE_TRADE_USD:.0f}")
        print()

    # paper / live mode — one-shot (interval=0) or continuous
    if args.interval == 0:
        run_scan(scanner, generator, paper, log_dir, monitor, live_trader=live_trader,
                 all_users=args.all_users)
        return

    mode_label = "live" if args.mode == "live" else "paper"
    print(f"  [{mode_label}] Scanning every {args.interval}s. Ctrl+C to stop.\n")
    scan_count = 0
    while True:
        scan_count += 1
        print(f"─── Scan #{scan_count} ────────────────────────────────────────────")
        try:
            run_scan(scanner, generator, paper, log_dir, monitor, live_trader=live_trader,
                     all_users=args.all_users)
        except RuntimeError:
            # Hard halt from live_trader (kill switch or gate failure) — exit cleanly
            sys.exit(1)
        except Exception as e:
            print(f"  Scan error: {e}", file=sys.stderr)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
