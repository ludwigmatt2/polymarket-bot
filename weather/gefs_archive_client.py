"""Historical NOAA GEFS ensemble puller — the second model in the live blend.

Same idea as ecmwf_archive_client.py (byte-range GRIB2 message fetch off a
free, continuously-retained S3 archive — verified live back to Aug 2025), but
GEFS's sidecar is the classic wgrib2 `.idx` text format, not JSON, and GEFS's
ensemble is 31 members (gec00 control + gep01..gep30 perturbed) instead of
ECMWF's 51. Keyed "gfs_seamless" to match the model name production's MOS
(HistoricalSkillCorrector) and MODEL_WEIGHTS already use.

Blending this in alongside ECMWF gives the backtest a real 2-model spread
instead of one model's own (underdispersed) internal perturbations — see the
KNOWN LIMITATION note in scripts/historical_backtest.py.
"""

from __future__ import annotations

import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import eccodes

_BASE = "https://noaa-gefs-pds.s3.amazonaws.com"
_UA = {"User-Agent": "Mozilla/5.0"}
_STEPS = list(range(0, 241, 3))
_MEMBERS = ["gec00"] + [f"gep{n:02d}" for n in range(1, 31)]
_VAR, _LEVEL = "TMP", "2 m above ground"

_index_cache: dict[tuple, list[dict]] = {}
# See ecmwf_archive_client._bytes_cache — same rationale: many cities share a
# calendar day's run/member, so cache raw bytes independent of (lat,lon).
_bytes_cache: dict[tuple, bytes] = {}


def clear_cache() -> None:
    """Drop cached message bytes — call between calendar days in a multi-city
    batch run so memory doesn't grow unbounded across the full backtest window."""
    _bytes_cache.clear()


def _get(url: str, headers: dict | None = None, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={**_UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _stem(run_date: date, run_hour: int, member: str, step: int) -> str:
    return (f"gefs.{run_date:%Y%m%d}/{run_hour:02d}/atmos/pgrb2sp25/"
            f"{member}.t{run_hour:02d}z.pgrb2s.0p25.f{step:03d}")


def _index(run_date: date, run_hour: int, member: str, step: int) -> list[dict]:
    key = (run_date.isoformat(), run_hour, member, step)
    if key in _index_cache:
        return _index_cache[key]
    url = f"{_BASE}/{_stem(run_date, run_hour, member, step)}.idx"
    try:
        body = _get(url).decode()
    except Exception:  # noqa: BLE001 — run/step not published
        _index_cache[key] = []
        return []
    entries = []
    for line in body.splitlines():
        if not line.strip():
            continue
        parts = line.split(":")
        if len(parts) < 5:
            continue
        entries.append({"offset": int(parts[1]), "var": parts[3], "level": parts[4]})
    _index_cache[key] = entries
    return entries


def _member_value(run_date: date, run_hour: int, member: str, step: int,
                   lat: float, lon: float) -> float | None:
    entries = _index(run_date, run_hour, member, step)
    target = next((i for i, e in enumerate(entries)
                    if e["var"] == _VAR and e["level"] == _LEVEL), None)
    if target is None:
        return None
    cache_key = (run_date.isoformat(), run_hour, member, step)
    data = _bytes_cache.get(cache_key)
    if data is None:
        off = entries[target]["offset"]
        length = entries[target + 1]["offset"] - off if target + 1 < len(entries) else None
        range_hdr = f"bytes={off}-{off + length - 1}" if length else f"bytes={off}-"
        url = f"{_BASE}/{_stem(run_date, run_hour, member, step)}"
        try:
            data = _get(url, headers={"Range": range_hdr})
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
    except Exception:  # noqa: BLE001
        return None


def step_members_c(run_date: date, run_hour: int, step: int,
                    lat: float, lon: float, max_workers: int = 12) -> dict[str, float]:
    """member_name -> 2m temp (°C) at (lat,lon) for one run+step. {} if not published."""
    out: dict[str, float] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(_member_value, run_date, run_hour, m, step, lat, lon): m
                for m in _MEMBERS}
        for fut in futs:
            v = fut.result()
            if v is not None:
                out[futs[fut]] = v
    return out


def daily_extreme_members(lat: float, lon: float, event_day: date, tz_name: str,
                           run_date: date, run_hour: int = 0,
                           kind: str = "max") -> list[float]:
    """One value per ensemble member: that member's forecast daily max/min (°C)
    for `event_day` local calendar day. Mirrors ecmwf_archive_client's logic."""
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = timezone.utc
    run_dt = datetime(run_date.year, run_date.month, run_date.day, run_hour,
                       tzinfo=timezone.utc)
    day_steps = []
    for step in _STEPS:
        valid = run_dt + timedelta(hours=step)
        if valid.astimezone(tz).date() == event_day:
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
