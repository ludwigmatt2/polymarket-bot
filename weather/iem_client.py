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

# ICAO → (IEM network, station sid, IANA tz, lat, lon, DWD MOSMIX WMO id).
# US ASOS = "{STATE}_ASOS" (one underscore) with sid = ICAO minus the leading K;
# international = "{CC}__ASOS" (two underscores) with sid = full ICAO. Coords are
# the station's own location (from the IEM network GeoJSON) — used to forecast AT
# the station, not the city. Seeded for the traded cities; extend as new stations
# appear. wmo = None where the station isn't in MOSMIX (verified 2026-09-22 —
# see docs/PHASE3_STATION_FORECAST_PLAN.md) or hasn't been looked up yet; never
# guess an id.
_STATION_REGISTRY: dict[str, tuple[str, str, str, float, float, str | None]] = {
    "KLGA": ("NY_ASOS", "LGA", "America/New_York", 40.7794, -73.8803, "72503"),   # NYC / LaGuardia
    "KMIA": ("FL_ASOS", "MIA", "America/New_York", 25.7880, -80.3169, "72202"),   # Miami Intl
    "KDFW": ("TX_ASOS", "DFW", "America/Chicago", 32.8968, -97.0380, "72259"),    # Dallas/Fort Worth (unused by markets; kept for MOS table)
    "KDAL": ("TX_ASOS", "DAL", "America/Chicago", 32.8471, -96.8518, None),       # Dallas Love Field — the station Polymarket ACTUALLY resolves on; NOT in MOSMIX (T6)
    "KATL": ("GA_ASOS", "ATL", "America/New_York", 33.6301, -84.4418, "72219"),   # Atlanta
    "RKSI": ("KR__ASOS", "RKSI", "Asia/Seoul", 37.4667, 126.4500, "47113"),       # Seoul / Incheon
    "VHHH": ("HK__ASOS", "VHHH", "Asia/Hong_Kong", 22.3094, 113.9219, "45007"),   # Hong Kong Intl
    "LFPB": ("FR__ASOS", "LFPB", "Europe/Paris", 48.9672, 2.4272, "07150"),       # Paris / Le Bourget
    "LFPG": ("FR__ASOS", "LFPG", "Europe/Paris", 49.0153, 2.5344, "07157"),       # Paris / Charles de Gaulle
    "LLBG": ("IL__ASOS", "LLBG", "Asia/Jerusalem", 32.0114, 34.8867, "40180"),    # Tel Aviv / Ben Gurion (NOAA-sourced markets, °C)
    "EGLC": ("GB__ASOS", "EGLC", "Europe/London", 51.5053, 0.0553, "P0478"),      # London City Airport (°F markets!)
    "RJTT": ("JP__ASOS", "RJTT", "Asia/Tokyo", 35.5533, 139.7811, "47671"),       # Tokyo / Haneda (°C)
    # Jul-8 registry expansion (scripts/discover_stations.py — IEM+WU verified):
    "LEMD": ("ES__ASOS", "LEMD", "Europe/Madrid", 40.4667, -3.5556, "08221"),     # Madrid / Barajas (°C)
    "OMDB": ("AE__ASOS", "OMDB", "Asia/Dubai", 25.2539, 55.3656, None),          # Dubai Intl (°F markets!); MOSMIX id not yet verified
    "WSSS": ("SG__ASOS", "WSSS", "Asia/Singapore", 1.3667, 103.9833, "48698"),    # Singapore / Changi (°C)
    "ZSPD": ("CN__ASOS", "ZSPD", "Asia/Shanghai", 31.1167, 121.7667, "58362"),    # Shanghai / Pudong (°C)
    # Canadian networks are CA_{PROVINCE}_ASOS (three segments — see is_us()).
    "CYYZ": ("CA_ON_ASOS", "CYYZ", "America/Toronto", 43.6772, -79.6306, "71624"),  # Toronto / Pearson (°C)
    # Jul-10 registry expansion (scripts/discover_stations.py — IEM+WU verified):
    "ZGSZ": ("CN__ASOS", "ZGSZ", "Asia/Shanghai", 22.5500, 114.1000, "59493"),  # Shenzhen Bao'an (°C)
    # Oct-25 expansion: 34 stations, paper-only until promoted (config.EXPANSION_STATIONS,
    # LIVE_PAPER_ONLY_STATIONS). IEM + aviationweather METAR verified; wmo None = not
    # looked up yet (never guess).
    "EDDM": ("DE__ASOS", "EDDM", "Europe/Berlin", 48.3583, 11.8092, None),  # Munich
    "EFHK": ("FI__ASOS", "EFHK", "Europe/Helsinki", 60.3172, 24.9633, None),  # Helsinki
    "EHAM": ("NL__ASOS", "EHAM", "Europe/Amsterdam", 52.3154, 4.7902, None),  # Amsterdam
    "EPWA": ("PL__ASOS", "EPWA", "Europe/Warsaw", 52.1628, 20.9611, None),  # Warsaw
    "FACT": ("ZA__ASOS", "FACT", "Africa/Johannesburg", -33.9667, 18.6, None),  # Cape Town
    "KAUS": ("TX_ASOS", "AUS", "America/Chicago", 30.183, -97.6799, None),  # Austin
    "KDEN": ("CO_ASOS", "DEN", "America/Denver", 39.8328, -104.6575, None),  # Denver
    "KHOU": ("TX_ASOS", "HOU", "America/Chicago", 29.6375, -95.2824, None),  # Houston
    "KLAX": ("CA_ASOS", "LAX", "America/Los_Angeles", 33.9382, -118.3865, None),  # Los Angeles
    "KORD": ("IL_ASOS", "ORD", "America/Chicago", 41.9602, -87.9316, None),  # Chicago
    "KSEA": ("WA_ASOS", "SEA", "America/Los_Angeles", 47.4447, -122.3144, None),  # Seattle
    "KSFO": ("CA_ASOS", "SFO", "America/Los_Angeles", 37.619, -122.3749, None),  # San Francisco
    "LIMC": ("IT__ASOS", "LIMC", "Europe/Rome", 45.63, 8.7231, None),  # Milan
    "LTAC": ("TR__ASOS", "LTAC", "Europe/Istanbul", 40.1281, 32.9951, None),  # Ankara
    "LTFM": ("TR__ASOS", "LTFM", "Europe/Istanbul", 41.2629, 28.7413, None),  # Istanbul
    "MMMX": ("MX__ASOS", "MMMX", "America/Mexico_City", 19.4363, -99.0721, None),  # Mexico City
    "MPMG": ("PA__ASOS", "MPMG", "America/Panama", 8.9833, -79.5167, None),  # Panama City
    "NZWN": ("NF__ASOS", "NZWN", "Pacific/Auckland", -41.3272, 174.8053, None),  # Wellington
    "OEJN": ("SA__ASOS", "OEJN", "Asia/Riyadh", 21.6598, 39.1222, None),  # Jeddah
    "OPKC": ("PK__ASOS", "OPKC", "Asia/Karachi", 24.8456, 67.1614, None),  # Karachi
    "RCSS": ("TW__ASOS", "RCSS", "Asia/Taipei", 25.0694, 121.5517, None),  # Taipei
    "RKPK": ("KR__ASOS", "RKPK", "Asia/Seoul", 35.1795, 128.9382, None),  # Busan
    "RPLL": ("PH__ASOS", "RPLL", "Asia/Manila", 14.5069, 121.0042, None),  # Manila
    "SAEZ": ("AR__ASOS", "SAEZ", "America/Argentina/Buenos_Aires", -34.8222, -58.5358, None),  # Buenos Aires
    "SBGR": ("BR__ASOS", "SBGR", "America/Sao_Paulo", -23.4321, -46.4695, None),  # Sao Paulo
    "UUWW": ("RU__ASOS", "UUWW", "Europe/Moscow", 55.5915, 37.2615, None),  # Moscow
    "VILK": ("IN__ASOS", "VILK", "Asia/Kolkata", 26.7606, 80.8893, None),  # Lucknow
    "WMKK": ("MY__ASOS", "WMKK", "Asia/Kuala_Lumpur", 2.7167, 101.7, None),  # Kuala Lumpur
    "ZBAA": ("CN__ASOS", "ZBAA", "Asia/Shanghai", 40.0741, 116.587, None),  # Beijing
    "ZGGG": ("CN__ASOS", "ZGGG", "Asia/Shanghai", 23.3964, 113.3008, None),  # Guangzhou
    "ZHHH": ("CN__ASOS", "ZHHH", "Asia/Shanghai", 30.62, 114.13, None),  # Wuhan
    "ZSQD": ("CN__ASOS", "ZSQD", "Asia/Shanghai", 36.0667, 120.3333, None),  # Qingdao
    "ZUCK": ("CN__ASOS", "ZUCK", "Asia/Chongqing", 29.52, 106.48, None),  # Chongqing
    "ZUUU": ("CN__ASOS", "ZUUU", "Asia/Chongqing", 30.6667, 104.0167, None),  # Chengdu
}


def station_meta(icao: str) -> dict | None:
    """Return {icao, network, sid, tz, lat, lon, wmo} for a known station, else
    None. wmo (DWD MOSMIX station id) may itself be None — see registry comment."""
    key = (icao or "").strip().upper()
    e = _STATION_REGISTRY.get(key)
    if not e:
        return None
    net, sid, tz, lat, lon, wmo = e
    return {"icao": key, "network": net, "sid": sid, "tz": tz, "lat": lat, "lon": lon, "wmo": wmo}


def icao_for_latlon(lat: float, lon: float, max_distance_km: float = 5.0) -> str | None:
    """Reverse lookup: the registry ICAO whose own coords are within
    max_distance_km, or None. market_scanner constructs every Location from
    this same registry's (lat, lon) (see station_meta), so this is a
    same-source match — a tight tolerance is correct here, unlike
    HistoricalSkillCorrector._nearest_city's fuzzy 100km nearest-city search."""
    from .city_bias import _haversine
    best, best_d = None, float("inf")
    for icao, e in _STATION_REGISTRY.items():
        d = _haversine(lat, lon, e[3], e[4])
        if d < best_d:
            best_d, best = d, icao
    return best if best is not None and best_d < max_distance_km else None


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
    °F. Returns [] for international stations (IEM has no international MOS).

    sts/ets MUST carry a UTC offset — IEM's validator rejects naive timestamps
    (pydantic timezone_aware error) the same way asos.py does (see metar_peak).
    A bare `except` previously turned that 422 into a silent [] for every call;
    narrowed here so a real regression fails loudly instead of looking like
    "no MOS for this station"."""
    sts = run_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    ets = (run_dt + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        data = json.loads(_get("/cgi-bin/request/mos.py", {
            "station": (icao or "").strip().upper(), "model": model,
            "sts": sts, "ets": ets, "format": "json",
        }))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(data, list):
        return data
    return data.get("data", []) if isinstance(data, dict) else []


# ── unit helpers ──────────────────────────────────────────────────────────────
def f_to_c(f: float) -> float:
    return (f - 32.0) * 5.0 / 9.0


def c_to_f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0
