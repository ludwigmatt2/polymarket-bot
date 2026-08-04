"""Historical ECMWF ensemble puller — the real forecast Open-Meteo can't replay.

Open-Meteo's ensemble API only retains ~3-4 days of past members (see
openmeteo_archive_depth memory), so nothing before that is walk-forward
backtestable. ECMWF's own free Open Data feed on AWS S3 has NOT been rolling
off since it launched (Apr 2024) — every daily run's full 51-member ENS GRIB2
is still there, verified live against an Aug-2025 run. Since Polymarket's
daily city-temperature markets themselves only go back to ~mid-2025, this
single free archive covers the bot's ENTIRE tradeable market history.

Each GRIB2 file bundles every variable/level/member for one run+lead-time
step (multi-GB). We never download a whole file: the sidecar `.index` lists
byte offsets per message, so we HTTP Range-GET only the "2t" (2m temperature)
message for each of the 51 members, decode it with eccodes, and pull the
nearest grid point to the target station.

Reconstructing a daily max/min needs several lead-time steps (ECMWF ENS at
0p25 ships 3-hourly out to 144h) spanning the station's LOCAL calendar day,
reduced per-member — the same "extreme over hourly steps" idea already used
live in weather/station_obs.py, just replayed against archived forecast
steps instead of archived observations.
"""

from __future__ import annotations

import json
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import eccodes

_BASE = "https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com"
_UA = {"User-Agent": "Mozilla/5.0"}
_STEPS = list(range(0, 145, 3))  # ENS 0p25 cadence out to 144h

_index_cache: dict[tuple, list[dict]] = {}
# Raw message bytes, keyed independent of (lat,lon) — when scaling to many
# cities, most share the same run/step on a given calendar day. Caching here
# means the 2nd+ city's request for that (run,step,member) skips the network
# fetch entirely and only pays for a cheap re-decode + nearest-point lookup.
# Caller clears this between calendar days to bound memory (see clear_cache()).
_bytes_cache: dict[tuple, bytes] = {}


def clear_cache() -> None:
    """Drop cached message bytes — call between calendar days in a multi-city
    batch run so memory doesn't grow unbounded across the full backtest window."""
    _bytes_cache.clear()


def _get(url: str, headers: dict | None = None, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={**_UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _index(run_date: date, run_hour: int, step: int) -> list[dict]:
    key = (run_date.isoformat(), run_hour, step)
    if key in _index_cache:
        return _index_cache[key]
    stem = f"{run_date:%Y%m%d}{run_hour:02d}0000-{step}h-enfo-ef"
    url = f"{_BASE}/{run_date:%Y%m%d}/{run_hour:02d}z/ifs/0p25/enfo/{stem}.index"
    try:
        body = _get(url).decode()
    except Exception:  # noqa: BLE001 — run/step not published (too old/new/missing)
        _index_cache[key] = []
        return []
    entries = [json.loads(line) for line in body.splitlines() if line.strip()]
    _index_cache[key] = entries
    return entries


def _member_value(run_date: date, run_hour: int, step: int, entry: dict,
                   lat: float, lon: float) -> float | None:
    number = entry.get("number", "cf")
    cache_key = (run_date.isoformat(), run_hour, step, number)
    data = _bytes_cache.get(cache_key)
    if data is None:
        stem = f"{run_date:%Y%m%d}{run_hour:02d}0000-{step}h-enfo-ef"
        url = f"{_BASE}/{run_date:%Y%m%d}/{run_hour:02d}z/ifs/0p25/enfo/{stem}.grib2"
        off, length = entry["_offset"], entry["_length"]
        try:
            data = _get(url, headers={"Range": f"bytes={off}-{off + length - 1}"})
        except Exception:  # noqa: BLE001
            return None
        _bytes_cache[cache_key] = data
    try:
        gid = eccodes.codes_new_from_message(data)
        try:
            nearest = eccodes.codes_grib_find_nearest(gid, lat, lon % 360)
            return float(nearest[0]["value"]) - 273.15  # K -> C
        finally:
            eccodes.codes_release(gid)
    except Exception:  # noqa: BLE001 — one bad message must not sink the whole pull
        return None


def step_members_c(run_date: date, run_hour: int, step: int,
                    lat: float, lon: float, max_workers: int = 12) -> dict[str, float]:
    """member_number -> 2m temp (°C) at (lat,lon) for one run+step. {} if the
    run/step isn't published (too recent, too old, or a genuine gap)."""
    entries = [e for e in _index(run_date, run_hour, step) if e.get("param") == "2t"]
    if not entries:
        return {}
    out: dict[str, float] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(_member_value, run_date, run_hour, step, e, lat, lon): e.get("number", "cf")
                for e in entries}
        for fut in futs:
            v = fut.result()
            if v is not None:
                out[futs[fut]] = v
    return out


def daily_extreme_members(lat: float, lon: float, event_day: date, tz_name: str,
                           run_date: date, run_hour: int = 0,
                           kind: str = "max") -> list[float]:
    """One value per ensemble member: that member's forecast daily max/min (°C)
    for `event_day` local calendar day, from the `run_date`/`run_hour` ECMWF ENS
    run. Steps are filtered to those whose VALID time falls on the local day
    (with a 1-step pad each side so a run issued near local midnight doesn't
    clip the day's true extreme), then reduced per-member."""
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = timezone.utc
    run_dt = datetime(run_date.year, run_date.month, run_date.day, run_hour,
                       tzinfo=timezone.utc)
    day_steps = []
    for step in _STEPS:
        valid = run_dt + timedelta(hours=step)
        local_day = valid.astimezone(tz).date()
        if local_day == event_day:
            day_steps.append(step)
    if not day_steps:
        return []
    pad = [s for s in (min(day_steps) - 3, max(day_steps) + 3) if s in _STEPS]
    steps = sorted(set(day_steps + pad))

    per_member: dict[str, list[float]] = {}
    for step in steps:
        vals = step_members_c(run_date, run_hour, step, lat, lon)
        for member, v in vals.items():
            per_member.setdefault(member, []).append(v)

    reducer = max if kind == "max" else min
    return [reducer(vs) for vs in per_member.values() if vs]
