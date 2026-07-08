"""Real-time station observations — the running daily extreme.

For a market scanned ON its event day, part of the outcome has already been
measured: the final daily max is max(observed-so-far, rest-of-day), so every
ensemble member can be clipped at the station's running extreme before
probabilities are computed (symmetrically with min for lows). Buckets entirely
below an already-observed high collapse toward P≈0/1 automatically.

This is the live feed informed traders watch — the flow Gate 9.5 crudely
refuses to fade on extreme "equal" prices. Now the bot reads the same
thermometer, from IEM's near-real-time METAR mirror (free, ~5–15 min latency).

Results are cached per (station, day, kind, UTC-hour) so a generator that
lives across hourly scans re-reads the feed at most once per hour per station.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from . import iem_client

_cache: dict[tuple, float | None] = {}


def local_event_date(res_dt: datetime, tz_name: str) -> date:
    """The event's calendar date in the station's LOCAL timezone (resolution
    deadlines are stored UTC and can sit on the adjacent date near midnight)."""
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = timezone.utc
    if res_dt.tzinfo is None:
        res_dt = res_dt.replace(tzinfo=timezone.utc)
    return res_dt.astimezone(tz).date()


def is_event_day(res_dt: datetime, tz_name: str, now: datetime | None = None) -> bool:
    """True when 'now' falls on the market's event day at the station."""
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = timezone.utc
    now = now or datetime.now(timezone.utc)
    return now.astimezone(tz).date() == local_event_date(res_dt, tz_name)


def running_extreme_c(icao: str, day: date, kind: str) -> float | None:
    """The station's observed running max/min (°C) for `day` so far, from the
    raw METAR stream. None when the feed has no obs (or on any fetch failure) —
    callers must treat None as 'feature stands down', never as 0."""
    hour_bucket = datetime.now(timezone.utc).strftime("%Y%m%dT%H")
    key = (icao, day.isoformat(), kind, hour_bucket)
    if key in _cache:
        return _cache[key]
    if len(_cache) > 512:  # hourly buckets expire naturally; just bound memory
        _cache.clear()
    try:
        v_f = iem_client.metar_peak(icao, day, kind)
    except Exception:  # noqa: BLE001 — obs are an enhancement, never a blocker
        v_f = None
    v_c = iem_client.f_to_c(v_f) if v_f is not None else None
    _cache[key] = v_c
    return v_c
