#!/usr/bin/env python3
"""
Weather Bot — Ensemble forecast vs. Polymarket weather market arbitrage.

Modes:
  scan     One-shot: scan markets, evaluate signals, print summary (no logging)
  paper    Continuous paper trading — scan + log signals (default)
  stats    Print paper trading dashboard and go-live gate status
  resolve  Interactively resolve outstanding paper trades with actual outcomes
  debug    Print full model internals: 7-day ensemble, scaling ratio, projected totals

Run:
  python weather_bot.py --mode scan
  python weather_bot.py --mode paper --interval 3600
  python weather_bot.py --mode stats
  python weather_bot.py --mode debug
  python weather_bot.py --mode debug --city Seoul
"""

import argparse
import calendar
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
from weather.market_scanner import WeatherMarketScanner
from weather.models import Signal
from weather.paper_trader import PaperTrader
from weather.probability_model import ProbabilityModel
from weather.signal_generator import SignalGenerator
from weather.weather_client import WeatherClient


def run_scan(scanner: WeatherMarketScanner, generator: SignalGenerator, paper: PaperTrader | None) -> list[Signal]:
    """Single scan cycle: find markets → evaluate signals → optionally log."""
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

    if paper and actionable:
        print(f"  [3/3] Logging {len(actionable)} paper trades...", end=" ", flush=True)
        logged = [paper.log_trade(s) for s in actionable]
        print(f"{sum(1 for t in logged if t)} logged")

    _print_scan_summary(signals, actionable, rejected)
    _write_signals_file(actionable)
    return signals


def _write_signals_file(actionable: list[Signal]) -> None:
    import json
    from pathlib import Path
    out = Path("logs/last_signals.json")
    out.write_text(json.dumps({
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
    }))


def _write_resolved_file(paper: "PaperTrader", resolved_count: int) -> None:
    import csv as _csv
    import json
    from pathlib import Path
    trades_path = Path("logs/paper_trades.csv")
    if not trades_path.exists():
        return
    with trades_path.open() as f:
        rows = list(_csv.DictReader(f))
    # Most recently resolved trades (by resolved_at timestamp)
    recently_resolved = sorted(
        [r for r in rows if r.get("resolved_at")],
        key=lambda x: x.get("resolved_at", ""), reverse=True
    )[:resolved_count]
    Path("logs/last_resolved.json").write_text(json.dumps({
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


def mode_arb(min_spread: float | None, limit: int) -> None:
    """
    Surface cross-venue arbitrage opportunities via the hosted pmxt Router.
    Requires PMXT_API_KEY in .env. Identity-matched markets only — same
    question, different venues. Net-profitable threshold is roughly 4%
    once round-trip fees are included.
    """
    import pmxt
    r = pmxt.Router()
    arbs = r.fetch_arbitrage(min_spread=min_spread, limit=limit, relations=["identity"])
    if not arbs:
        print(f"  No identity-matched arb opportunities found (min_spread={min_spread}).")
        return

    print(f"  {len(arbs)} cross-venue arb opportunities (identity match):\n")
    print(f"  {'spread':>7}  {'buy':>10}  {'@':>5}  {'sell':>10}  {'@':>5}  {'conf':>5}  Question")
    print(f"  {'-'*7}  {'-'*10}  {'-'*5}  {'-'*10}  {'-'*5}  {'-'*5}  {'-'*60}")
    for a in arbs:
        print(f"  {a.spread:>6.1%}  {a.buy_venue:>10}  {a.buy_price:>5.3f}  "
              f"{a.sell_venue:>10}  {a.sell_price:>5.3f}  {a.confidence:>5.2f}  "
              f"{a.market_a.title[:60]}")


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
    parser.add_argument("--mode", choices=["scan", "paper", "stats", "resolve", "auto-resolve", "debug", "backtest", "backtest-temp", "arb"],
                        default="paper", help="Operating mode (default: paper)")
    parser.add_argument("--interval", type=int, default=3600,
                        help="Re-scan interval in seconds for paper mode (default: 3600)")
    parser.add_argument("--city", type=str, default=None,
                        help="Filter markets by city name (debug mode only)")
    parser.add_argument("--min-spread", type=float, default=None,
                        help="Minimum cross-venue spread filter for arb mode (e.g. 0.04 for 4%%)")
    parser.add_argument("--limit", type=int, default=20,
                        help="Max results for arb mode (default: 20)")
    args = parser.parse_args()

    print(f"Weather Bot  [mode={args.mode}]")
    print()

    if args.mode == "stats":
        mode_stats(PaperTrader())
        return

    if args.mode == "resolve":
        mode_resolve(PaperTrader())
        return

    if args.mode == "auto-resolve":
        paper = PaperTrader()
        client = WeatherClient()
        resolved_count, skipped = paper.auto_resolve(client)
        print(f"  Auto-resolved {resolved_count} trade(s).  {skipped} skipped (no archive data or missing fields).")
        if resolved_count:
            paper.print_dashboard()
            _write_resolved_file(paper, resolved_count)
        return

    if args.mode == "backtest":
        mode_backtest(WeatherClient())
        return

    if args.mode == "backtest-temp":
        mode_backtest_temp(WeatherClient())
        return

    if args.mode == "arb":
        mode_arb(min_spread=args.min_spread, limit=args.limit)
        return

    # Build components
    client = WeatherClient()
    model = ProbabilityModel()
    scanner = WeatherMarketScanner()
    generator = SignalGenerator(model=model, client=client)
    paper = PaperTrader() if args.mode == "paper" else None

    if args.mode == "debug":
        mode_debug(scanner, generator, city_filter=args.city)
        return

    if args.mode == "scan":
        run_scan(scanner, generator, paper=None)
        return

    # paper mode — one-shot (interval=0) or continuous
    if args.interval == 0:
        run_scan(scanner, generator, paper)
        return

    print(f"  Scanning every {args.interval}s. Ctrl+C to stop.\n")
    scan_count = 0
    while True:
        scan_count += 1
        print(f"─── Scan #{scan_count} ────────────────────────────────────────────")
        try:
            run_scan(scanner, generator, paper)
        except Exception as e:
            print(f"  Scan error: {e}", file=sys.stderr)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
