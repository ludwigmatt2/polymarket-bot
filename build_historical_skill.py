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

CITY_BIAS_CSV = Path("logs/city_bias.csv")
SKILL_PATH = Path(HISTORICAL_SKILL_PATH)

LEADS = [1, 2, 3, 4, 5]
# (skill metric → how a daily value is reduced from hourly temperature_2m)
TEMP_METRICS = {"temperature_2m_max": max, "temperature_2m_min": min}
START_DATE = date(2024, 1, 1)   # Previous Runs retention floor


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
        sys.exit("logs/city_bias.csv not found — needed for the city list.")
    seen, cities = set(), []
    for row in csv.DictReader(open(CITY_BIAS_CSV)):
        key = (round(float(row["lat"]), 2), round(float(row["lon"]), 2))
        if key in seen:
            continue
        seen.add(key)
        cities.append({"city": row["city"], "lat": float(row["lat"]), "lon": float(row["lon"])})
    return cities


def _fetch_previous_runs(lat: float, lon: float, start: date, end: date) -> tuple[list[str], dict[int, list]]:
    """Hourly temperature_2m_previous_day1..5 over [start,end]. Returns (times, {lead: hourly_values})."""
    hourly_vars = ",".join(f"temperature_2m_previous_day{l}" for l in LEADS)
    params = {
        "latitude": lat, "longitude": lon, "hourly": hourly_vars,
        "start_date": start.isoformat(), "end_date": end.isoformat(), "timezone": "auto",
    }
    r = requests.get(OPEN_METEO_PREVIOUS_RUNS_URL, params=params, timeout=OPEN_METEO_REQUEST_TIMEOUT * 4)
    r.raise_for_status()
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
    r = requests.get(OPEN_METEO_ARCHIVE_URL, params=params, timeout=OPEN_METEO_REQUEST_TIMEOUT * 4)
    r.raise_for_status()
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

    for i, tgt in enumerate(targets, 1):
        try:
            entry, errors = build(tgt)
        except Exception as exc:
            print(f"  [{i}/{len(targets)}] {tgt['label']:<14} FAILED: {exc}")
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
        return

    SKILL_PATH.parent.mkdir(parents=True, exist_ok=True)
    SKILL_PATH.write_text(json.dumps(table, indent=2))
    print(f"\nWrote {SKILL_PATH}  ({len(table)} cities)")


if __name__ == "__main__":
    main()
