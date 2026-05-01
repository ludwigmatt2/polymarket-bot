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


def mode_debug(scanner: WeatherMarketScanner, generator: SignalGenerator, client: WeatherClient, city_filter: str | None) -> None:
    """
    Print full model internals for each tradeable market:
    7-day ensemble sums, historical scaling ratio, projected monthly total, P vs market price.
    """
    print("  Scanning markets...", end=" ", flush=True)
    markets = scanner.scan()
    print(f"{len(markets)} tradeable\n")

    for market in markets:
        title = market.title
        if city_filter and city_filter.lower() not in title.lower():
            continue

        sig = generator.evaluate(market)

        print(f"{'─'*70}")
        print(f"  {title[:68]}")
        print(f"  Market ID : {market.market_id}")
        print(f"  Metric    : {market.metric}  |  Threshold: {market.threshold}mm  "
              f"Direction: {market.direction}"
              + (f"  High: {market.threshold_high}mm" if market.threshold_high else ""))
        print(f"  Market P  : {market.yes_price:.3f} YES  |  Liquidity: ${market.liquidity_usd:.0f}")
        print()

        if market.forecast_start_date is not None:
            dbg = client.debug_monthly_forecast(
                location=market.location,
                month_start=market.forecast_start_date,
                metric=market.metric,
            )
            loc = dbg["location"]
            print(f"  Location  : {loc['city']} ({loc['lat']:.2f}, {loc['lon']:.2f})")
            print(f"  Scaling   : {dbg['scaling_ratio']}x  "
                  f"(7-day ensemble × ratio = projected full-month total)")
            print(f"  Members   : {dbg['total_members']} total")
            print(f"  Projected : mean={dbg['projected_mean_mm']}mm  "
                  f"min={dbg['projected_min_mm']}mm  max={dbg['projected_max_mm']}mm")
            print()
            for mdl, stats in dbg["per_model"].items():
                print(f"    {mdl:<20}  n={stats['n_members']}  "
                      f"7d=[{stats['7d_min_mm']}-{stats['7d_max_mm']}mm avg={stats['7d_mean_mm']}mm]  "
                      f"proj=[{stats['projected_min_mm']}-{stats['projected_max_mm']}mm avg={stats['projected_mean_mm']}mm]")
        else:
            print(f"  Location  : {market.location.city} ({market.location.lat:.2f}, {market.location.lon:.2f})")
            print(f"  (single-date market, no scaling ratio)")

        print()
        gate = "PASS" if sig.quality_gate_passed else f"FAIL ({sig.rejection_reason})"
        print(f"  Model P   : {sig.model_p:.4f}   Edge: {sig.edge_pp:.1%}   Dir: {sig.direction}   "
              f"Spread: {sig.ensemble_spread:.4f}   Gate: {gate}")
        print()


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
    parser.add_argument("--mode", choices=["scan", "paper", "stats", "resolve", "debug"],
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

    # Build components
    client = WeatherClient()
    model = ProbabilityModel()
    scanner = WeatherMarketScanner()
    generator = SignalGenerator(model=model, client=client)
    paper = PaperTrader() if args.mode == "paper" else None

    if args.mode == "debug":
        mode_debug(scanner, generator, client, city_filter=args.city)
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
