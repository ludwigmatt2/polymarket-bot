"""
Weather model calibration report.

Compares model_p vs climatological baseline vs actual outcomes on all resolved
paper trades.

Outputs:
  1. Overall Brier scores + BSS (model skill vs climatology)
  2. Calibration table — model_p buckets → actual outcome rate
  3. Edge reliability  — edge buckets → win rate
  4. Direction breakdown (equal / range / above / below)
  5. logs/calibration_report.csv — full per-trade detail
"""

from __future__ import annotations

import calendar
import csv
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# ── Load project modules ──────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))
from weather.models import Location
from weather.weather_client import WeatherClient

# ── Config ────────────────────────────────────────────────────────────────────
TRADES_CSV   = Path("logs/paper_trades.csv")
REPORT_CSV   = Path("logs/calibration_report.csv")
WINDOW_DAYS  = 7   # ±days around target day-of-year when building clim distribution
TRAIN_YEARS  = 7   # how many prior years


# ── Data loading ──────────────────────────────────────────────────────────────

def load_resolved() -> list[dict]:
    if not TRADES_CSV.exists():
        sys.exit("No paper_trades.csv found.")
    rows = list(csv.DictReader(open(TRADES_CSV)))
    return [r for r in rows if r.get("actual_outcome") not in (None, "", "None")]


# ── Climatological probability ────────────────────────────────────────────────

def clim_p(
    client: WeatherClient,
    lat: float,
    lon: float,
    target_date: date,
    metric: str,
    threshold: float,
    direction: str,
    threshold_high: float | None,
) -> float | None:
    """
    Empirical P(outcome) from ±WINDOW_DAYS historical values across TRAIN_YEARS
    prior years. Same method as TempBacktester, extended for all direction types.
    """
    loc = Location(city="", lat=lat, lon=lon, timezone="UTC")
    train_vals: list[float] = []

    for years_back in range(1, TRAIN_YEARS + 1):
        yr = target_date.year - years_back
        try:
            w_start = target_date.replace(year=yr) - timedelta(days=WINDOW_DAYS)
            w_end   = target_date.replace(year=yr) + timedelta(days=WINDOW_DAYS)
        except ValueError:
            continue
        last_day    = calendar.monthrange(yr, target_date.month)[1]
        fetch_start = max(w_start, date(yr, target_date.month, 1))
        fetch_end   = min(w_end,   date(yr, target_date.month, last_day))
        vals = client.get_archive_daily_values(loc, fetch_start, fetch_end, metric)
        train_vals.extend(vals)

    if len(train_vals) < 5:
        return None

    n = len(train_vals)
    if direction == "above":
        return sum(1 for v in train_vals if v > threshold) / n
    if direction == "below":
        return sum(1 for v in train_vals if v < threshold) / n
    if direction == "equal":
        return sum(1 for v in train_vals if threshold - 0.5 <= v <= threshold + 0.5) / n
    if direction == "range" and threshold_high is not None:
        return sum(1 for v in train_vals if threshold <= v <= threshold_high) / n
    return None


# ── Scoring helpers ───────────────────────────────────────────────────────────

def brier(p: float, outcome: int) -> float:
    return (p - outcome) ** 2

def bss(mean_brier_model: float, mean_brier_ref: float) -> float:
    if mean_brier_ref == 0:
        return 0.0
    return 1.0 - mean_brier_model / mean_brier_ref

def bucket(value: float, edges: list[float]) -> int:
    for i, e in enumerate(edges):
        if value < e:
            return i
    return len(edges)


# ── Formatting helpers ────────────────────────────────────────────────────────

def bar(ratio: float, width: int = 20) -> str:
    filled = round(ratio * width)
    return "█" * filled + "░" * (width - filled)

def pct(v: float | None) -> str:
    return f"{v:.1%}" if v is not None else "  n/a "


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print("Loading resolved trades...", flush=True)
    trades = load_resolved()
    print(f"  {len(trades)} resolved trades found.")
    print()

    client = WeatherClient()

    # ── Fetch climatological probabilities ────────────────────────────────────
    print(f"Fetching climatological baselines ({len(trades)} trades × {TRAIN_YEARS} years)...")
    print("  This takes ~2 min — one archive call per trade per year.\n")

    enriched: list[dict] = []
    skipped = 0

    for i, t in enumerate(trades, 1):
        try:
            lat        = float(t["lat"])
            lon        = float(t["lon"])
            threshold  = float(t["threshold"])
            threshold_high = float(t["threshold_high"]) if t.get("threshold_high") else None
            model_p    = float(t["model_p"])
            actual     = int(t["actual_outcome"])
            direction  = t["weather_direction"]
            metric     = t["metric"]
            res_date   = datetime.fromisoformat(t["resolution_date"]).date()
            edge       = float(t["edge_pp"])
        except (ValueError, KeyError):
            skipped += 1
            continue

        p_clim = clim_p(client, lat, lon, res_date, metric, threshold, direction, threshold_high)
        if p_clim is None:
            skipped += 1
            continue

        b_model = brier(model_p, actual)
        b_clim  = brier(p_clim,  actual)

        enriched.append({
            "trade_id":       t["trade_id"],
            "market_title":   t["market_title"][:60],
            "resolution_date": res_date.isoformat(),
            "direction":      direction,
            "metric":         metric,
            "model_p":        model_p,
            "p_clim":         p_clim,
            "actual":         actual,
            "edge":           edge,
            "brier_model":    b_model,
            "brier_clim":     b_clim,
            "model_wins":     int(b_model < b_clim),
        })

        # Progress dot every 10
        if i % 10 == 0:
            print(f"  {i}/{len(trades)} done...", flush=True)

    print(f"\n  Done. {len(enriched)} trades scored, {skipped} skipped (missing fields).")
    print()

    if not enriched:
        print("Nothing to report.")
        return

    # ── Save CSV ──────────────────────────────────────────────────────────────
    REPORT_CSV.parent.mkdir(exist_ok=True)
    with open(REPORT_CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(enriched[0].keys()))
        w.writeheader()
        w.writerows(enriched)
    print(f"Detail saved → {REPORT_CSV}\n")

    # ── Section 1: Overall stats ──────────────────────────────────────────────
    n             = len(enriched)
    mean_b_model  = sum(e["brier_model"] for e in enriched) / n
    mean_b_clim   = sum(e["brier_clim"]  for e in enriched) / n
    mean_b_naive  = 0.25   # uninformed 50/50 baseline
    bss_vs_clim   = bss(mean_b_model, mean_b_clim)
    bss_vs_naive  = bss(mean_b_model, mean_b_naive)
    model_beats   = sum(e["model_wins"] for e in enriched)
    actual_rate   = sum(e["actual"] for e in enriched) / n

    print("═" * 58)
    print("  WEATHER MODEL CALIBRATION REPORT")
    print("═" * 58)
    print(f"  Trades analysed : {n}")
    print(f"  Actual YES rate : {actual_rate:.1%}  (market type skew expected)")
    print()
    print(f"  Brier score — model : {mean_b_model:.4f}")
    print(f"  Brier score — clim  : {mean_b_clim:.4f}")
    print(f"  Brier score — naive : {mean_b_naive:.4f}  (50/50 baseline)")
    print()
    skill_icon = "✅" if bss_vs_clim > 0 else "❌"
    print(f"  BSS vs climatology  : {skill_icon} {bss_vs_clim:+.3f}  (>0 = beats history)")
    print(f"  BSS vs coin-flip    : {'✅' if bss_vs_naive > 0 else '❌'} {bss_vs_naive:+.3f}")
    print(f"  Model beats clim    : {model_beats}/{n} trades  ({model_beats/n:.0%})")
    print()

    # ── Section 2: Calibration table ─────────────────────────────────────────
    print("─" * 58)
    print("  CALIBRATION  (model_p bucket → actual outcome rate)")
    print("─" * 58)
    cal_edges = [0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 1.01]
    cal_labels = ["0–10%", "10–20%", "20–30%", "30–40%", "40–50%", "50–60%", "60–70%", "70%+"]
    cal_buckets: dict[int, list] = defaultdict(list)
    for e in enriched:
        b = bucket(e["model_p"], cal_edges)
        cal_buckets[b].append(e["actual"])

    print(f"  {'Predicted':>10}  {'n':>4}  {'Actual':>8}  {'Calibration bar'}")
    for i, label in enumerate(cal_labels):
        vals = cal_buckets.get(i, [])
        if not vals:
            continue
        actual_frac = sum(vals) / len(vals)
        midpoint    = (cal_edges[i-1] if i > 0 else 0.0 + cal_edges[i]) / 2
        diff        = actual_frac - (cal_edges[i-1] if i > 0 else 0)
        icon = "✅" if abs(actual_frac - (sum(cal_edges[i-1:i+1])/2 if i > 0 else cal_edges[0]/2)) < 0.10 else "⚠️"
        print(f"  {label:>10}  {len(vals):>4}  {actual_frac:>7.1%}  {bar(actual_frac)} {icon}")
    print()

    # ── Section 3: Edge reliability ───────────────────────────────────────────
    print("─" * 58)
    print("  EDGE RELIABILITY  (edge bucket → actual win rate)")
    print("  (win = model's direction was correct)")
    print("─" * 58)

    edge_edges  = [0.15, 0.25, 0.35, 0.50, 1.01]
    edge_labels = ["<15%", "15–25%", "25–35%", "35–50%", "50%+"]

    # Re-load directions from original trades for edge analysis
    orig = {t["trade_id"]: t["direction"] for t in trades}
    for e in enriched:
        td = orig.get(e["trade_id"], "")
        actual = e["actual"]
        e["win"] = int((td == "YES" and actual == 1) or (td == "NO" and actual == 0))
        e["trade_direction"] = td

    edge_buckets: dict[int, list] = defaultdict(list)
    for e in enriched:
        b = bucket(e["edge"], edge_edges)
        edge_buckets[b].append(e["win"])

    print(f"  {'Edge':>10}  {'n':>4}  {'Win rate':>9}  {'Win rate bar'}")
    for i, label in enumerate(edge_labels):
        vals = edge_buckets.get(i, [])
        if not vals:
            continue
        win_rate = sum(vals) / len(vals)
        icon = "✅" if win_rate >= 0.55 else ("⚠️" if win_rate >= 0.45 else "❌")
        print(f"  {label:>10}  {len(vals):>4}  {win_rate:>8.1%}  {bar(win_rate)} {icon}")
    print()

    # ── Section 4: Direction breakdown ───────────────────────────────────────
    print("─" * 58)
    print("  BY DIRECTION TYPE")
    print("─" * 58)
    dir_groups: dict[str, list] = defaultdict(list)
    for e in enriched:
        dir_groups[e["direction"]].append(e)

    print(f"  {'Type':>8}  {'n':>4}  {'Brier model':>12}  {'Brier clim':>11}  {'BSS':>7}")
    for d in sorted(dir_groups):
        grp = dir_groups[d]
        mb  = sum(e["brier_model"] for e in grp) / len(grp)
        cb  = sum(e["brier_clim"]  for e in grp) / len(grp)
        s   = bss(mb, cb)
        icon = "✅" if s > 0 else "❌"
        print(f"  {d:>8}  {len(grp):>4}  {mb:>12.4f}  {cb:>11.4f}  {icon} {s:+.3f}")
    print()

    # ── Section 5: Worst calls (highest model Brier) ──────────────────────────
    worst = sorted(enriched, key=lambda e: e["brier_model"], reverse=True)[:5]
    print("─" * 58)
    print("  5 WORST CALLS  (highest Brier = most confidently wrong)")
    print("─" * 58)
    for e in worst:
        td  = orig.get(e["trade_id"], "?")
        print(f"  {td:>3} | model={e['model_p']:.0%} clim={e['p_clim']:.0%} actual={e['actual']} "
              f"brier={e['brier_model']:.3f}")
        print(f"      {e['market_title']}")
    print()
    print("═" * 58)


if __name__ == "__main__":
    main()
