"""
Phase 1 — build the historical forecast-skill table (non-parametric MOS).

For each (city, metric, lead_day, month) we estimate the systematic forecast error
    error = forecast − actual
from the Open-Meteo **Previous Runs API** (lead-time-specific past forecasts, back
to ~Jan 2024) scored against ERA5 archive actuals. The HistoricalSkillCorrector
later shifts ensemble members by −mean_error before computing raw_p, removing the
model's systematic warm/cold bias at each lead and season.

Why Previous Runs (deterministic) and not the ensemble archive: free Open-Meteo
keeps ensemble MEMBERS for only ~3 days, so the live ensemble can't be replayed
historically. The deterministic lead-N forecast ≈ ensemble mean, so its error is a
sound basis for the member shift. See memory: openmeteo_archive_depth.

`previous_dayN` exists only for HOURLY variables, so daily max/min at lead N are
reconstructed as the max/min of that day's 24 hourly `temperature_2m_previous_dayN`.

Output: logs/historical_skill.json
    {city_key: {"city","lat","lon","metrics":{metric:{lead:{month:{mean_error,std_error,n}}}}}}
month "0" is the all-months aggregate fallback.

Usage:
    python build_historical_skill.py                 # build full table + validate
    python build_historical_skill.py --validate-only # just print out-of-sample MAE reduction
    python build_historical_skill.py --max-cities 3  # quick smoke build
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

from weather.config import (
    HISTORICAL_SKILL_PATH,
    MIN_SKILL_OBS,
    OPEN_METEO_ARCHIVE_URL,
    OPEN_METEO_PREVIOUS_RUNS_URL,
    OPEN_METEO_REQUEST_TIMEOUT,
)

from weather.paths import DATA_DIR

# Both sides of the rebuild live on the data volume (DATA_DIR/logs). The city list
# used to be a bare relative Path("logs/…"), which resolved against the CWD — so on
# the VPS, where DATA_DIR is /opt/polymarket-bot/data, the rebuild wrote its table to
# data/logs/ but looked for its INPUT in the stale repo-root logs/ and died with
# "city_bias.csv not found". The table has not been rebuilt since 2026-07-08 for
# exactly this reason.
CITY_BIAS_CSV = DATA_DIR / "logs" / "city_bias.csv"
# Write to the data volume (DATA_DIR/logs) — the same place the corrector reads.
SKILL_PATH = DATA_DIR / HISTORICAL_SKILL_PATH

LEADS = [1, 2, 3, 4, 5]
# (skill metric → how a daily value is reduced from hourly temperature_2m)
TEMP_METRICS = {"temperature_2m_max": max, "temperature_2m_min": min}
START_DATE = date(2024, 1, 1)   # Previous Runs retention floor
# A rebuild may not replace the live table with less than this fraction of its
# current coverage — see the degenerate-write guard in main().
MIN_REBUILD_COVERAGE = 0.9


# ── Pure logic (unit-tested, no network) ────────────────────────────────────────

def daily_from_hourly(times: list[str], values: list[float | None], reducer) -> dict[str, float]:
    """Reduce hourly values to one value per local day via `reducer` (max/min)."""
    buckets: dict[str, list[float]] = defaultdict(list)
    for t, v in zip(times, values):
        if v is not None:
            buckets[t[:10]].append(float(v))
    return {d: reducer(vs) for d, vs in buckets.items() if vs}


def collect_errors(
    forecast_by_lead: dict[int, dict[str, float]],
    actual_by_date: dict[str, float],
) -> dict[int, dict[int, list[float]]]:
    """
    error = forecast − actual, bucketed as {lead: {month: [errors]}}.
    month 0 is added as the all-months aggregate.
    """
    out: dict[int, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    for lead, fc in forecast_by_lead.items():
        for d, f in fc.items():
            a = actual_by_date.get(d)
            if a is None:
                continue
            err = f - a
            month = int(d[5:7])
            out[lead][month].append(err)
            out[lead][0].append(err)
    return out


def aggregate_cell(errors: list[float]) -> dict | None:
    """mean/std/n for one (lead, month) cell."""
    n = len(errors)
    if n == 0:
        return None
    return {
        "mean_error": round(statistics.mean(errors), 3),
        "std_error": round(statistics.pstdev(errors), 3) if n > 1 else 0.0,
        "n": n,
    }


def build_metric_stats(errors_by_lead_month: dict[int, dict[int, list[float]]]) -> dict:
    """{lead: {month: {mean_error,std_error,n}}} from raw error buckets."""
    stats: dict[str, dict] = {}
    for lead, by_month in errors_by_lead_month.items():
        lead_stats = {}
        for month, errs in by_month.items():
            cell = aggregate_cell(errs)
            if cell:
                lead_stats[str(month)] = cell
        if lead_stats:
            stats[str(lead)] = lead_stats
    return stats


def validate_mae_reduction(
    errors_by_lead: dict[int, list[float]],
    split: float = 0.7,
) -> dict[int, dict]:
    """
    Out-of-sample check: fit mean_error on the first `split` of each lead's error
    series, apply it to the held-out tail, and compare MAE before vs after. A
    positive reduction means the MOS correction genuinely removes systematic error.
    Returns {lead: {mae_before, mae_after, reduction_pct, n_test}}.
    """
    result = {}
    for lead, all_errs in errors_by_lead.items():
        if len(all_errs) < 20:
            continue
        cut = int(len(all_errs) * split)
        train, test = all_errs[:cut], all_errs[cut:]
        if not train or not test:
            continue
        bias = statistics.mean(train)
        mae_before = statistics.mean(abs(e) for e in test)
        mae_after = statistics.mean(abs(e - bias) for e in test)
        result[lead] = {
            "mae_before": round(mae_before, 3),
            "mae_after": round(mae_after, 3),
            "reduction_pct": round(100 * (mae_before - mae_after) / mae_before, 1) if mae_before else 0.0,
            "n_test": len(test),
        }
    return result


def validate_correction_levels(
    city_error_structs: list[dict[int, dict[int, list[float]]]],
    min_cell: int = MIN_SKILL_OBS,
    split: float = 0.7,
) -> dict:
    """
    Out-of-sample comparison of three correction levels, pooled over cities:
      raw      : no correction                       MAE = mean|e|
      flat     : subtract one per-city mean          MAE = mean|e − flat_mean|   (≈ Phase-2 city bias)
      seasonal : subtract per-(lead,month) mean      MAE = mean|e − cell_mean|   (Phase-1 MOS)

    Each cell is split chronologically (train = first `split`); means are fit on
    train only and scored on the held-out tail. The decision: seasonal must beat
    flat (not just raw) for month-keyed MOS to be worth shipping over Phase 2.
    Returns {raw_mae, flat_mae, seasonal_mae, n_test, seasonal_vs_flat_pct, seasonal_vs_raw_pct}.
    """
    raw_pool: list[float] = []
    flat_pool: list[float] = []
    seasonal_pool: list[float] = []

    for struct in city_error_structs:
        # collect this city's per-cell train/test (skip month-0 aggregate)
        cells = []  # (train, test)
        train_all: list[float] = []
        for lead, by_month in struct.items():
            for month, errs in by_month.items():
                if month == 0 or len(errs) < min_cell:
                    continue
                cut = int(len(errs) * split)
                train, test = errs[:cut], errs[cut:]
                if len(train) < 5 or not test:
                    continue
                cells.append((train, test))
                train_all.extend(train)
        if not train_all:
            continue
        flat_mean = statistics.mean(train_all)
        for train, test in cells:
            cell_mean = statistics.mean(train)
            for e in test:
                raw_pool.append(abs(e))
                flat_pool.append(abs(e - flat_mean))
                seasonal_pool.append(abs(e - cell_mean))

    if not raw_pool:
        return {"n_test": 0}
    raw_mae = statistics.mean(raw_pool)
    flat_mae = statistics.mean(flat_pool)
    seasonal_mae = statistics.mean(seasonal_pool)
    return {
        "raw_mae": round(raw_mae, 3),
        "flat_mae": round(flat_mae, 3),
        "seasonal_mae": round(seasonal_mae, 3),
        "n_test": len(raw_pool),
        "seasonal_vs_flat_pct": round(100 * (flat_mae - seasonal_mae) / flat_mae, 1) if flat_mae else 0.0,
        "seasonal_vs_raw_pct": round(100 * (raw_mae - seasonal_mae) / raw_mae, 1) if raw_mae else 0.0,
    }


# ── Network fetch ────────────────────────────────────────────────────────────────

def _load_cities() -> list[dict]:
    if not CITY_BIAS_CSV.exists():
        sys.exit(f"{CITY_BIAS_CSV} not found — needed for the city list. "
                 "Set RAILWAY_VOLUME_MOUNT_PATH if the data volume is elsewhere.")
    seen, cities = set(), []
    for row in csv.DictReader(open(CITY_BIAS_CSV)):
        key = (round(float(row["lat"]), 2), round(float(row["lon"]), 2))
        if key in seen:
            continue
        seen.add(key)
        cities.append({"city": row["city"], "lat": float(row["lat"]), "lon": float(row["lon"])})
    return cities


def _get_with_backoff(url: str, params: dict, *, attempts: int = 5):
    """GET with exponential backoff on 429/5xx.

    Open-Meteo rate-limits per minute. The rebuild fires two multi-year requests per
    city back to back with no pacing, so an unlucky start (or a validate-only run
    immediately before, as the systemd unit does) 429s on EVERY city — a whole-table
    failure from a transient limit. Sep 15 2026: that happened and the run still
    exited 0, writing an empty table over the live one.
    """
    delay = 5.0
    for attempt in range(1, attempts + 1):
        r = requests.get(url, params=params, timeout=OPEN_METEO_REQUEST_TIMEOUT * 4)
        if r.status_code != 429 and r.status_code < 500:
            r.raise_for_status()
            return r
        if attempt == attempts:
            r.raise_for_status()
        wait = float(r.headers.get("Retry-After") or delay)
        print(f"      rate-limited ({r.status_code}), retry {attempt}/{attempts - 1} in {wait:.0f}s",
              flush=True)
        time.sleep(wait)
        delay = min(delay * 2, 120.0)
    raise RuntimeError("unreachable")


def _fetch_previous_runs(lat: float, lon: float, start: date, end: date) -> tuple[list[str], dict[int, list]]:
    """Hourly temperature_2m_previous_day1..5 over [start,end]. Returns (times, {lead: hourly_values})."""
    hourly_vars = ",".join(f"temperature_2m_previous_day{l}" for l in LEADS)
    params = {
        "latitude": lat, "longitude": lon, "hourly": hourly_vars,
        "start_date": start.isoformat(), "end_date": end.isoformat(), "timezone": "auto",
    }
    r = _get_with_backoff(OPEN_METEO_PREVIOUS_RUNS_URL, params)
    h = r.json().get("hourly", {})
    times = h.get("time", [])
    by_lead = {l: h.get(f"temperature_2m_previous_day{l}", []) for l in LEADS}
    return times, by_lead


def _fetch_archive_daily(lat: float, lon: float, start: date, end: date) -> dict[str, dict[str, float]]:
    """Archive daily temperature_2m_max & _min, date-aligned. Returns {metric: {date: value}}."""
    params = {
        "latitude": lat, "longitude": lon,
        "daily": "temperature_2m_max,temperature_2m_min",
        "start_date": start.isoformat(), "end_date": end.isoformat(), "timezone": "auto",
    }
    r = _get_with_backoff(OPEN_METEO_ARCHIVE_URL, params)
    d = r.json().get("daily", {})
    times = d.get("time", [])
    out = {}
    for metric in TEMP_METRICS:
        vals = d.get(metric, [])
        out[metric] = {t: float(v) for t, v in zip(times, vals) if v is not None}
    return out


def _city_key(lat: float, lon: float) -> str:
    return f"{round(lat, 2)},{round(lon, 2)}"


def build_city(city: dict, start: date, end: date) -> tuple[dict, dict]:
    """Build per-metric stats + raw error buckets (for validation) for one city."""
    times, fc_hourly_by_lead = _fetch_previous_runs(city["lat"], city["lon"], start, end)
    actual = _fetch_archive_daily(city["lat"], city["lon"], start, end)

    metrics_stats = {}
    metrics_errors = {}
    for metric, reducer in TEMP_METRICS.items():
        fc_by_lead = {
            lead: daily_from_hourly(times, fc_hourly_by_lead.get(lead, []), reducer)
            for lead in LEADS
        }
        errors = collect_errors(fc_by_lead, actual.get(metric, {}))
        metrics_stats[metric] = build_metric_stats(errors)
        metrics_errors[metric] = errors

    entry = {"city": city["city"], "lat": city["lat"], "lon": city["lon"], "metrics": metrics_stats}
    return entry, metrics_errors


def build_station(icao: str, start: date, end: date) -> tuple[dict, dict]:
    """Phase 2 MOS for one resolving station: forecast AT the station's coords,
    corrected toward the station's OWN reading (IEM), not the ERA5 grid — so the
    member shift removes the forecast's bias against the actual resolving thermometer."""
    from weather import iem_client
    m = iem_client.station_meta(icao)
    if not m:
        raise ValueError(f"unknown station {icao}")
    times, fc_hourly_by_lead = _fetch_previous_runs(m["lat"], m["lon"], start, end)
    iem_daily = iem_client.daily_range(icao, start, end)
    actual = {
        "temperature_2m_max": {d: iem_client.f_to_c(v["max_f"])
                               for d, v in iem_daily.items() if v.get("max_f") is not None},
        "temperature_2m_min": {d: iem_client.f_to_c(v["min_f"])
                               for d, v in iem_daily.items() if v.get("min_f") is not None},
    }
    metrics_stats, metrics_errors = {}, {}
    for metric, reducer in TEMP_METRICS.items():
        fc_by_lead = {lead: daily_from_hourly(times, fc_hourly_by_lead.get(lead, []), reducer)
                      for lead in LEADS}
        errors = collect_errors(fc_by_lead, actual.get(metric, {}))
        metrics_stats[metric] = build_metric_stats(errors)
        metrics_errors[metric] = errors
    entry = {"city": icao, "lat": m["lat"], "lon": m["lon"], "metrics": metrics_stats}
    return entry, metrics_errors


def main() -> None:
    ap = argparse.ArgumentParser(description="Build historical forecast-skill (MOS) table")
    ap.add_argument("--max-cities", type=int, default=0, help="limit cities (smoke test)")
    ap.add_argument("--validate-only", action="store_true", help="don't write JSON, just report MAE reduction")
    ap.add_argument("--allow-shrink", action="store_true",
                    help="permit writing a table with fewer entries than the live one "
                         "(normally refused — a shrunk table means targets failed)")
    ap.add_argument("--fail-on-no-ship", action="store_true",
                    help="with --validate-only, exit non-zero if no metric beats flat "
                         "bias by >1%% (lets a scheduled rebuild gate on the verdict)")
    ap.add_argument("--stations", action="store_true",
                    help="Phase 2: build station-keyed MOS from IEM actuals (merged into existing table)")
    ap.add_argument("--start", default=START_DATE.isoformat())
    args = ap.parse_args()

    start = date.fromisoformat(args.start)
    end = date.today() - timedelta(days=1)

    if args.stations:
        from weather.iem_client import _STATION_REGISTRY
        icaos = sorted(_STATION_REGISTRY)
        targets = [{"icao": ic, "label": ic} for ic in icaos]
        build = lambda t: build_station(t["icao"], start, end)  # noqa: E731
        # merge into the existing table so non-station cities keep their MOS
        table: dict[str, dict] = json.loads(SKILL_PATH.read_text()) if SKILL_PATH.exists() else {}
        print(f"Building STATION MOS for {len(targets)} stations, {start} → {end}\n")
    else:
        cities = _load_cities()
        if args.max_cities:
            cities = cities[:args.max_cities]
        targets = [{**c, "label": c["city"]} for c in cities]
        build = lambda t: build_city(t, start, end)  # noqa: E731
        table = {}
        print(f"Building skill for {len(targets)} cities, {start} → {end}\n")

    # per-metric list of per-target error structs {lead:{month:[errs]}} for validation
    city_structs: dict[str, list] = defaultdict(list)

    n_failed = 0
    for i, tgt in enumerate(targets, 1):
        try:
            entry, errors = build(tgt)
        except Exception as exc:
            print(f"  [{i}/{len(targets)}] {tgt['label']:<14} FAILED: {exc}")
            n_failed += 1
            continue
        table[_city_key(entry["lat"], entry["lon"])] = entry
        for metric, errs in errors.items():
            city_structs[metric].append(errs)
        all_cells = [cell for m in entry["metrics"].values() for lead in m.values() for cell in lead.values()]
        n_obs = sum(cell["n"] for cell in all_cells)
        print(f"  [{i}/{len(targets)}] {tgt['label']:<14} cells={len(all_cells)} obs={n_obs}")

    # ── Out-of-sample validation (the acceptance gate) ───────────────────────────
    # The decision metric: does per-(lead,month) MOS beat the flat per-city mean
    # (≈ Phase-2 city bias)? If seasonal_vs_flat is not clearly positive, month-keyed
    # MOS adds nothing over Phase 2 and should NOT ship.
    print("\n══════════ MOS validation: raw vs flat-bias vs seasonal MOS (MAE °C) ══════════")
    ship = {}
    for metric, structs in city_structs.items():
        lv = validate_correction_levels(structs)
        ship[metric] = lv
        if lv.get("n_test"):
            print(f"\n  {metric}  (n_test={lv['n_test']})")
            print(f"    raw      MAE {lv['raw_mae']:.3f}")
            print(f"    flat     MAE {lv['flat_mae']:.3f}   ({lv['seasonal_vs_flat_pct']:+.1f}% is seasonal vs flat)")
            print(f"    seasonal MAE {lv['seasonal_mae']:.3f}   (vs raw {lv['seasonal_vs_raw_pct']:+.1f}%)")
            verdict = "SHIP" if lv["seasonal_vs_flat_pct"] > 1.0 else "do NOT ship (no gain over flat bias)"
            print(f"    → {verdict}")
        else:
            print(f"\n  {metric}: insufficient data")
    print("\n══════════════════════════════════════════════════════════════════════════════")
    print(f"  MIN_SKILL_OBS gate = {MIN_SKILL_OBS} (cells below this fall back at inference)")

    if args.validate_only:
        print("\n(validate-only: JSON not written)")
        # Exit non-zero when nothing earned SHIP, so an automated rebuild can gate
        # on this (deploy/systemd/pmbot-mos-rebuild.service runs it as ExecStartPre).
        # Off by default: a human running --validate-only wants the report, not a
        # failing shell.
        if args.fail_on_no_ship and not any(
            (v.get("seasonal_vs_flat_pct") or 0.0) > 1.0 for v in ship.values()
        ):
            sys.exit("no metric beat flat bias by >1% — refusing to ship this table")
        return

    # ── Degenerate-write guard ───────────────────────────────────────────────────
    # This table feeds live trading decisions. On 2026-09-15 a transient Open-Meteo
    # 429 storm failed all 15 cities, and the run wrote `{}` over a good 30-city
    # table and exited 0 — the live model silently lost its MOS. Never again: a
    # rebuild that lost targets is a FAILED rebuild, not a smaller table.
    #
    # --fail-on-no-ship gates on FIT QUALITY (is the correction any good) and is a
    # separate question from this, which gates on COVERAGE (did we actually get the
    # data). The 429 storm passed the quality gate vacuously, with zero cities.
    if n_failed:
        print(f"\n{n_failed}/{len(targets)} targets FAILED to build.")
    if not table:
        sys.exit("refusing to write an EMPTY skill table — every target failed "
                 "(transient rate-limit or network outage; rerun later)")
    existing = {}
    if SKILL_PATH.exists():
        try:
            existing = json.loads(SKILL_PATH.read_text())
        except (OSError, ValueError):
            existing = {}
    if existing and not args.allow_shrink and len(table) < len(existing) * MIN_REBUILD_COVERAGE:
        sys.exit(f"refusing to shrink the live skill table from {len(existing)} to "
                 f"{len(table)} entries ({n_failed} target(s) failed). Rerun when the "
                 f"upstream is healthy, or pass --allow-shrink if the loss is intended.")

    SKILL_PATH.parent.mkdir(parents=True, exist_ok=True)
    SKILL_PATH.write_text(json.dumps(table, indent=2))
    print(f"\nWrote {SKILL_PATH}  ({len(table)} cities)")


if __name__ == "__main__":
    main()
