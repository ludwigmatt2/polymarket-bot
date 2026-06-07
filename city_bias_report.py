"""
City bias + market integrity report.

For every resolved paper trade:
  1. Re-fetch actual temperature from Open-Meteo archive
  2. Re-compute outcome with our logic → integrity check vs stored outcome
  3. Compute residual (actual_temp - threshold) → city temperature bias

Outputs:
  - Integrity issues (stored outcome doesn't match re-computed)
  - Per-city bias table with statistical reliability flag
  - Shanghai / extreme-priced market analysis
  - logs/city_bias.csv  (consumed by CityBiasCorrector)
"""

from __future__ import annotations

import csv
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, stdev

sys.path.insert(0, str(Path(__file__).parent))
from weather.models import Location
from weather.paper_trader import _evaluate_outcome
from weather.weather_client import WeatherClient

TRADES_CSV  = Path("logs/paper_trades.csv")
BIAS_CSV    = Path("logs/city_bias.csv")
MIN_TRADES_FOR_CORRECTION = 8   # don't apply correction below this
DAMPING_FULL_N = 20             # full confidence at this many trades

CITY_RE = re.compile(r"temperature in ([A-Za-z ]+?) on")


def parse_city(title: str) -> str:
    m = CITY_RE.search(title)
    return m.group(1).strip() if m else "Unknown"


def market_correct(r: dict) -> bool:
    return (r["stored_outcome"] == 1 and r["mkt_yes"] > 0.5) or (r["stored_outcome"] == 0 and r["mkt_yes"] <= 0.5)

def bar(v: float, scale: float = 1.0, width: int = 20) -> str:
    filled = min(round(abs(v) / scale * width), width)
    char = "▶" if v >= 0 else "◀"
    return char * filled + "·" * (width - filled)


def main() -> None:
    if not TRADES_CSV.exists():
        sys.exit("No paper_trades.csv found.")

    rows = list(csv.DictReader(open(TRADES_CSV)))
    resolved = [r for r in rows if r.get("actual_outcome") in ("0", "1")]
    print(f"Resolved trades to analyse: {len(resolved)}")
    print(f"Fetching {len(resolved)} archive temperatures...\n")

    client = WeatherClient()

    results: list[dict] = []
    integrity_issues: list[dict] = []

    for i, t in enumerate(resolved, 1):
        try:
            lat   = float(t["lat"])
            lon   = float(t["lon"])
            thr   = float(t["threshold"])
            thr_h = float(t["threshold_high"]) if t.get("threshold_high") else None
            w_dir = t["weather_direction"]
            res_dt = datetime.fromisoformat(t["resolution_date"]).date()
            stored_outcome = int(t["actual_outcome"])
            city  = parse_city(t["market_title"])
            metric = t["metric"]
            entry_price = float(t["entry_price"])
            trade_dir   = t["direction"]
            model_p     = float(t["model_p"])
        except (ValueError, KeyError):
            continue

        # This is a TEMPERATURE bias report (residual is in °C and the offset is
        # subtracted from temperature thresholds). Precipitation trades have mm
        # thresholds, so pooling their residuals (e.g. 5mm − 150mm = −145) poisons
        # the per-city mean. Restrict to temperature metrics.
        if metric not in ("temperature_2m_max", "temperature_2m_min"):
            continue

        loc = Location(city=city, lat=lat, lon=lon, timezone="UTC")
        actual_temp = client.get_historical_actual(loc, res_dt, metric)
        if actual_temp is None:
            continue

        recomputed = int(_evaluate_outcome(actual_temp, thr, w_dir, thr_h))
        residual   = actual_temp - thr   # positive = warmer than threshold

        # Market YES price (reconstruct from entry_price + direction)
        mkt_yes = entry_price if trade_dir == "YES" else (1.0 - entry_price)

        results.append({
            "city":           city,
            "date":           res_dt.isoformat(),
            "metric":         metric,
            "threshold":      thr,
            "actual_temp":    actual_temp,
            "residual":       residual,
            "direction":      w_dir,
            "stored_outcome": stored_outcome,
            "recomputed":     recomputed,
            "integrity_ok":   stored_outcome == recomputed,
            "model_p":        model_p,
            "mkt_yes":        mkt_yes,
            "lat":            lat,
            "lon":            lon,
        })

        if stored_outcome != recomputed:
            integrity_issues.append(results[-1])

        if i % 20 == 0:
            print(f"  {i}/{len(resolved)} done...", flush=True)

    print(f"\n  Done. {len(results)} temperatures fetched, {len(resolved) - len(results)} skipped.\n")

    # ── Section 1: Integrity check ────────────────────────────────────────────
    print("═" * 60)
    print("  1. INTEGRITY CHECK")
    print("═" * 60)
    if not integrity_issues:
        print("  ✅ All stored outcomes match re-computed archive values.\n")
    else:
        print(f"  ⚠️  {len(integrity_issues)} MISMATCHES (stored outcome ≠ archive re-compute):\n")
        for r in integrity_issues:
            print(f"  {r['city']} {r['date']} | threshold={r['threshold']} | actual={r['actual_temp']:.1f} | "
                  f"stored={r['stored_outcome']} recomputed={r['recomputed']}")
        print()

    # ── Section 2: City bias table ────────────────────────────────────────────
    print("═" * 60)
    print("  2. CITY TEMPERATURE BIAS  (actual_temp − threshold)")
    print("     Positive = actual warmer than threshold (model runs cold)")
    print("     Negative = actual colder than threshold (model runs warm)")
    print("═" * 60)

    by_city: dict[str, list] = defaultdict(list)
    for r in results:
        by_city[r["city"]].append(r["residual"])

    print(f"  {'City':<15}  {'n':>4}  {'Mean bias':>10}  {'Stdev':>7}  {'Bar':>22}  {'Action'}")
    for city, residuals in sorted(by_city.items(), key=lambda x: abs(mean(x[1])), reverse=True):
        n       = len(residuals)
        m       = mean(residuals)
        sd      = stdev(residuals) if n > 1 else 0.0
        damped  = m * min(n / DAMPING_FULL_N, 1.0)
        reliable = n >= MIN_TRADES_FOR_CORRECTION
        action  = f"apply {damped:+.2f}°C" if reliable else "too few trades"
        flag    = "✅" if reliable and abs(m) >= 0.5 else ("⚠️" if reliable else "  ")
        print(f"  {city:<15}  {n:>4}  {m:>+9.2f}°C  {sd:>6.2f}  {bar(m, 3.0):>22}  {flag} {action}")
    print()

    # ── Section 3: Extreme market price analysis ──────────────────────────────
    print("═" * 60)
    print("  3. EXTREME MARKET PRICE ANALYSIS  (mkt_yes > 80% or < 20%)")
    print("     These are cases where the crowd was very confident.")
    print("═" * 60)

    extreme = [r for r in results if r["mkt_yes"] > 0.80 or r["mkt_yes"] < 0.20]
    model_right  = sum(1 for r in extreme if r["stored_outcome"] == int(r["model_p"] > 0.5))
    market_right = sum(1 for r in extreme if market_correct(r))

    print(f"  Extreme-priced trades: {len(extreme)}")
    print(f"  Market was correct:    {market_right}/{len(extreme)} ({market_right/max(len(extreme),1):.0%})")
    print(f"  Model was correct:     {model_right}/{len(extreme)}  ({model_right/max(len(extreme),1):.0%})\n")

    # By city breakdown for extreme trades
    by_city_ext: dict[str, list] = defaultdict(list)
    for r in extreme:
        by_city_ext[r["city"]].append(r)

    print(f"  {'City':<15}  {'n':>4}  {'Mkt correct':>12}  {'Model correct':>14}  Note")
    for city, trades in sorted(by_city_ext.items(), key=lambda x: -len(x[1])):
        mkt_ok   = sum(1 for r in trades if market_correct(r))
        model_ok = sum(1 for r in trades if r["stored_outcome"] == int(r["model_p"] > 0.5))
        note = "⚠️ market knows more" if mkt_ok > model_ok else ""
        print(f"  {city:<15}  {len(trades):>4}  {mkt_ok:>5}/{len(trades):<5}  {model_ok:>6}/{len(trades):<5}  {note}")
    print()

    # ── Section 4: Shanghai deep-dive ─────────────────────────────────────────
    shanghai = [r for r in results if r["city"] == "Shanghai"]
    if shanghai:
        print("═" * 60)
        print("  4. SHANGHAI DEEP DIVE")
        print("═" * 60)
        for r in shanghai:
            gap = r["actual_temp"] - r["threshold"]
            print(f"  {r['date']} | threshold={r['threshold']}°C actual={r['actual_temp']:.1f}°C "
                  f"gap={gap:+.1f} | mkt_yes={r['mkt_yes']:.0%} model_p={r['model_p']:.0%} "
                  f"outcome={r['stored_outcome']} integrity={'✅' if r['integrity_ok'] else '❌'}")
        print()

    # ── Write city_bias.csv (Phase 3: seasonal cells + month=0 all-season) ────
    # Every observation lands in two cells: its own month (1–12) and month 0 (the
    # all-season fallback that CityBiasCorrector uses when a month cell is sparse).
    cells: dict[tuple, list] = defaultdict(list)   # (lat, lon, month) -> residuals
    city_of: dict[tuple, str] = {}
    for r in results:
        key = (round(r["lat"], 2), round(r["lon"], 2))
        city_of[key] = r["city"]
        m = int(r["date"][5:7])
        cells[(key[0], key[1], m)].append(r["residual"])
        cells[(key[0], key[1], 0)].append(r["residual"])

    with open(BIAS_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["city", "lat", "lon", "month", "n", "mean_bias_c", "damped_bias_c", "reliable"])
        writer.writeheader()
        for (lat, lon, month), residuals in sorted(cells.items()):
            n = len(residuals)
            mb = mean(residuals)
            damped = mb * min(n / DAMPING_FULL_N, 1.0)
            writer.writerow({
                "city":          city_of[(lat, lon)],
                "lat":           lat,
                "lon":           lon,
                "month":         month,
                "n":             n,
                "mean_bias_c":   round(mb, 3),
                "damped_bias_c": round(damped, 3),
                "reliable":      int(n >= MIN_TRADES_FOR_CORRECTION),
            })

    n_season_cells = sum(1 for (la, lo, mo), t in cells.items() if mo != 0 and len(t) >= MIN_TRADES_FOR_CORRECTION)
    n_locations = len({(la, lo) for (la, lo, mo) in cells})
    print(f"City bias data written → {BIAS_CSV}")
    print(f"({n_locations} locations, {n_season_cells} reliable seasonal cells)\n")


if __name__ == "__main__":
    main()
