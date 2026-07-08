"""Iowa Environmental Mesonet (IEM) client — station-observation ground truth.

Polymarket resolves each daily-temperature market off a named AIRPORT STATION's
finalized daily max/min (via Weather Underground), whole degrees, the station's
local calendar day. IEM mirrors the same ASOS observations, so this module lets
the bot use the *station's* reading — what actually pays — instead of Open-Meteo's
gridded reanalysis.

Two views of the day's temperature are exposed, because they can disagree by ~1°F
and which one matches Wunderground is empirical (validate before trusting):
  * daily_maxmin()  — the DSM/CLI 2-minute-average official value (whole °F for
                      US NWS sites; IEM's own recompute for international sites).
  * metar_peak()    — the peak of the raw METAR obs over the local day (closer to
                      what Wunderground's "daily high" sometimes displays).

International sites have no DSM → daily_maxmin() is a recompute and can differ from
the exchange's resolving value; callers should treat them as provisional.
"""

from __future__ import annotations

import csv as _csv
import io
import json
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

_BASE = "https://mesonet.agron.iastate.edu"
_UA = {"User-Agent": "Mozilla/5.0"}
_MIN_INTERVAL_S = 1.05  # IEM requests ≤ 1 req/s per IP
_last_call = [0.0]

# ICAO → (IEM network, station sid, IANA tz, lat, lon). US ASOS = "{STATE}_ASOS"
# (one underscore) with sid = ICAO minus the leading K; international = "{CC}__ASOS"
# (two underscores) with sid = full ICAO. Coords are the station's own location
# (from the IEM network GeoJSON) — used to forecast AT the station, not the city.
# Seeded for the traded cities; extend as new stations appear.
_STATION_REGISTRY: dict[str, tuple[str, str, str, float, float]] = {
    "KLGA": ("NY_ASOS", "LGA", "America/New_York", 40.7794, -73.8803),   # NYC / LaGuardia
    "KMIA": ("FL_ASOS", "MIA", "America/New_York", 25.7880, -80.3169),   # Miami Intl
    "KDFW": ("TX_ASOS", "DFW", "America/Chicago", 32.8968, -97.0380),    # Dallas/Fort Worth (unused by markets; kept for MOS table)
    "KDAL": ("TX_ASOS", "DAL", "America/Chicago", 32.8471, -96.8518),    # Dallas Love Field — the station Polymarket ACTUALLY resolves on
    "KATL": ("GA_ASOS", "ATL", "America/New_York", 33.6301, -84.4418),   # Atlanta
    "RKSI": ("KR__ASOS", "RKSI", "Asia/Seoul", 37.4667, 126.4500),       # Seoul / Incheon
    "VHHH": ("HK__ASOS", "VHHH", "Asia/Hong_Kong", 22.3094, 113.9219),   # Hong Kong Intl
    "LFPB": ("FR__ASOS", "LFPB", "Europe/Paris", 48.9672, 2.4272),       # Paris / Le Bourget
    "LFPG": ("FR__ASOS", "LFPG", "Europe/Paris", 49.0153, 2.5344),       # Paris / Charles de Gaulle
    "LLBG": ("IL__ASOS", "LLBG", "Asia/Jerusalem", 32.0114, 34.8867),    # Tel Aviv / Ben Gurion (NOAA-sourced markets, °C)
    "EGLC": ("GB__ASOS", "EGLC", "Europe/London", 51.5053, 0.0553),      # London City Airport (°F markets!)
    "RJTT": ("JP__ASOS", "RJTT", "Asia/Tokyo", 35.5533, 139.7811),       # Tokyo / Haneda (°C)
    # Jul-8 registry expansion (scripts/discover_stations.py — IEM+WU verified):
    "LEMD": ("ES__ASOS", "LEMD", "Europe/Madrid", 40.4667, -3.5556),     # Madrid / Barajas (°C)
    "OMDB": ("AE__ASOS", "OMDB", "Asia/Dubai", 25.2539, 55.3656),        # Dubai Intl (°F markets!)
    "WSSS": ("SG__ASOS", "WSSS", "Asia/Singapore", 1.3667, 103.9833),    # Singapore / Changi (°C)
    "ZSPD": ("CN__ASOS", "ZSPD", "Asia/Shanghai", 31.1167, 121.7667),    # Shanghai / Pudong (°C)
    # Canadian networks are CA_{PROVINCE}_ASOS (three segments — see is_us()).
    "CYYZ": ("CA_ON_ASOS", "CYYZ", "America/Toronto", 43.6772, -79.6306),  # Toronto / Pearson (°C)
}


def station_meta(icao: str) -> dict | None:
    """Return {icao, network, sid, tz, lat, lon} for a known station, else None."""
    key = (icao or "").strip().upper()
    e = _STATION_REGISTRY.get(key)
    if not e:
        return None
    net, sid, tz, lat, lon = e
    return {"icao": key, "network": net, "sid": sid, "tz": tz, "lat": lat, "lon": lon}


def is_us(icao: str) -> bool:
    """True for CONUS/US ASOS networks — the DSM-backed sites. US networks are
    exactly two segments ({STATE}_ASOS, incl. California's CA_ASOS); international
    are {CC}__ASOS (empty middle segment) and Canada is CA_{PROV}_ASOS (three
    segments) — the old "no double underscore" test misread Canada as US."""
    m = station_meta(icao)
    return bool(m and len(m["network"].split("_")) == 2)


def _throttle() -> None:
    gap = time.monotonic() - _last_call[0]
    if gap < _MIN_INTERVAL_S:
        time.sleep(_MIN_INTERVAL_S - gap)
    _last_call[0] = time.monotonic()


def _get(path: str, params: dict, retries: int = 3) -> str:
    url = f"{_BASE}{path}?{urllib.parse.urlencode(params)}"
    last: Exception | None = None
    for i in range(retries):
        _throttle()
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=_UA), timeout=20) as r:
                return r.read().decode()
        except Exception as e:  # noqa: BLE001 — retry with backoff
            last = e
            time.sleep(1.0 * (i + 1))
    raise last if last else RuntimeError("IEM request failed")


def daily_maxmin(icao: str, day: date) -> dict | None:
    """DSM/CLI daily max & min in °F (US: official 2-min-avg; intl: recompute).
    Returns {"max_f": float|None, "min_f": float|None} or None if unavailable."""
    return daily_range(icao, day, day).get(day.isoformat())


def daily_range(icao: str, start: date, end: date) -> dict[str, dict]:
    """DSM daily max/min °F over [start, end] in one call, for backtest/MOS building.
    Returns {'YYYY-MM-DD': {'max_f': float|None, 'min_f': float|None}} (empty on fail)."""
    m = station_meta(icao)
    if not m:
        return {}
    try:
        arr = json.loads(_get("/cgi-bin/request/daily.py", {
            "sts": start.isoformat(), "ets": end.isoformat(),
            "network": m["network"], "stations": m["sid"],
            "var": "max_temp_f,min_temp_f", "format": "json",
        }))
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, dict] = {}
    for row in arr if isinstance(arr, list) else []:
        d = str(row.get("day", ""))[:10]
        if d:
            out[d] = {"max_f": row.get("max_temp_f"), "min_f": row.get("min_temp_f")}
    return out


def metar_peak(icao: str, day: date, kind: str = "max") -> float | None:
    """Peak (kind='max') or trough (kind='min') air temp °F from the raw METAR
    stream over the station's LOCAL calendar `day`. None if no obs."""
    m = station_meta(icao)
    if not m:
        return None
    tz = ZoneInfo(m["tz"])
    start = datetime(day.year, day.month, day.day, tzinfo=tz)
    end = start + timedelta(days=1)
    try:
        # sts/ets MUST carry a UTC offset — IEM's validator rejects naive
        # timestamps (pydantic timezone_aware error) and the old %H:%M format
        # made this fetch silently return None (caught Jul 8 wiring the
        # running-extreme feature; also the WU-fallback path in station_truth).
        txt = _get("/cgi-bin/request/asos.py", {
            "station": m["sid"], "data": "tmpf", "tz": m["tz"],
            "sts": start.isoformat(timespec="minutes"),
            "ets": end.isoformat(timespec="minutes"),
            "format": "onlycomma", "missing": "empty",
        })
    except Exception:  # noqa: BLE001
        return None
    vals: list[float] = []
    for r in _csv.reader(io.StringIO(txt)):
        if len(r) >= 3:
            try:
                vals.append(float(r[2]))
            except ValueError:
                pass  # header / missing
    if not vals:
        return None
    return max(vals) if kind == "max" else min(vals)


def mos_forecast(icao: str, run_dt: datetime, model: str = "MEX") -> list[dict]:
    """NWS MOS station forecast via IEM (US-only). `n_x` per row = the 12-h max/min
    °F. Returns [] for international stations (IEM has no international MOS)."""
    sts = run_dt.strftime("%Y-%m-%dT%H:%M")
    ets = (run_dt + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")
    try:
        data = json.loads(_get("/cgi-bin/request/mos.py", {
            "station": (icao or "").strip().upper(), "model": model,
            "sts": sts, "ets": ets, "format": "json",
        }))
    except Exception:  # noqa: BLE001
        return []
    if isinstance(data, list):
        return data
    return data.get("data", []) if isinstance(data, dict) else []


# ── unit helpers ──────────────────────────────────────────────────────────────
def f_to_c(f: float) -> float:
    return (f - 32.0) * 5.0 / 9.0


def c_to_f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0
