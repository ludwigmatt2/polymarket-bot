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
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from weather.backtest import Backtester, BacktestResult, default_test_suite, summarize
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
    return signals


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

    print(f"  {'City':<12} {'Y/M':<8} {'thr':>5}  {'actual':>7}  {'pred':>6}  {'σ':>5}  {'P_clim':>6}  {'P_mod':>6}  {'B_clim':>6}  {'B_mod':>6}  out")
    print(f"  {'-'*12} {'-'*8} {'-'*5}  {'-'*7}  {'-'*6}  {'-'*5}  {'-'*6}  {'-'*6}  {'-'*6}  {'-'*6}  ---")
    for r in results:
        c = r.case
        print(f"  {c.city:<12} {c.test_year}/{c.month:02d}  "
              f"{r.threshold_used:>5.0f}  {r.actual_total:>7.1f}  {r.model_point_pred:>6.1f}  {r.sigma:>5.1f}  "
              f"{r.p_climatology:>6.3f}  {r.p_model:>6.3f}  "
              f"{r.brier_climatology:>6.3f}  {r.brier_model:>6.3f}  {r.actual_binary}")

    s = summarize(results)
    print()
    print("  ═══════════════════════════════════════════════════════════════════")
    print(f"  N = {s['n']} cases")
    print(f"  Mean Brier — Climatology : {s['mean_brier_climatology']:.4f}")
    print(f"  Mean Brier — Model       : {s['mean_brier_model']:.4f}")
    print(f"  Brier Skill Score        : {s['brier_skill_score_vs_climatology']:+.4f}  "
          f"(>0 means model beats climatology)")
    if s['model_beats_clim']:
        print("  ✓ Model has skill — beats climatology baseline")
    else:
        print("  ✗ Model has NO skill — climatology is at least as good")
    print("  ═══════════════════════════════════════════════════════════════════")

    _print_breakdowns(results)
    _save_results_csv(results, Path("logs/backtest_results.csv"))


def _print_breakdowns(results: list[BacktestResult]) -> None:
    print()
    print("  Per-city Brier Skill Score:")
    by_city: dict[str, list[BacktestResult]] = {}
    for r in results:
        by_city.setdefault(r.case.city, []).append(r)
    for city, rs in by_city.items():
        s = summarize(rs)
        flag = "✓" if s["model_beats_clim"] else "✗"
        print(f"    {flag} {city:<12} n={s['n']:<3}  clim={s['mean_brier_climatology']:.3f}  "
              f"model={s['mean_brier_model']:.3f}  BSS={s['brier_skill_score_vs_climatology']:+.3f}")

    print("\n  Per-month Brier Skill Score:")
    by_month: dict[int, list[BacktestResult]] = {}
    for r in results:
        by_month.setdefault(r.case.month, []).append(r)
    for m in sorted(by_month):
        s = summarize(by_month[m])
        flag = "✓" if s["model_beats_clim"] else "✗"
        print(f"    {flag} month={m:<2}     n={s['n']:<3}  clim={s['mean_brier_climatology']:.3f}  "
              f"model={s['mean_brier_model']:.3f}  BSS={s['brier_skill_score_vs_climatology']:+.3f}")


def _save_results_csv(results: list[BacktestResult], path: Path) -> None:
    import csv
    path.parent.mkdir(exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["city", "metric", "year", "month", "threshold", "actual_total",
                    "actual_binary", "model_pred", "sigma", "p_climatology", "p_model",
                    "brier_climatology", "brier_model", "train_n"])
        for r in results:
            w.writerow([r.case.city, r.case.metric, r.case.test_year, r.case.month,
                        r.threshold_used, r.actual_total, r.actual_binary,
                        r.model_point_pred, r.sigma, r.p_climatology, r.p_model,
                        r.brier_climatology, r.brier_model, r.train_n])
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
    parser.add_argument("--mode", choices=["scan", "paper", "stats", "resolve", "debug", "backtest"],
                        default="paper", help="Operating mode (default: paper)")
    parser.add_argument("--interval", type=int, default=3600,
                        help="Re-scan interval in seconds for paper mode (default: 3600)")
    parser.add_argument("--city", type=str, default=None,
                        help="Filter markets by city name (debug mode only)")
    args = parser.parse_args()

    print(f"Weather Bot  [mode={args.mode}]")
    print()

    if args.mode == "stats":
        mode_stats(PaperTrader())
        return

    if args.mode == "resolve":
        mode_resolve(PaperTrader())
        return

    if args.mode == "backtest":
        mode_backtest(WeatherClient())
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

    # paper mode — continuous
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
