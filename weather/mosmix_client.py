"""DWD MOSMIX client — station-calibrated statistical forecasts, globally.

The bot forecasts an Open-Meteo grid cell; Polymarket resolves off an airport
thermometer (see iem_client's module docstring). DWD's MOSMIX_L is a free,
global, already-computed statistical forecast FOR that exact thermometer, and
it ships its own per-hour uncertainty (E_TTT, the standard error of TTT) — the
only roadmap item that adds new information rather than reprocessing what we
already have. See docs/PHASE3_STATION_FORECAST_PLAN.md for the full audit.

DWD publishes no availability SLA, so a fetch failure (station not covered,
outage, malformed KMZ) must stand the feature down — return None — never raise.
"""

from __future__ import annotations

import io
import json
import time
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

_BASE = "https://opendata.dwd.de/weather/local_forecasts/mos/MOSMIX_L/single_stations"
_UA = {"User-Agent": "Mozilla/5.0"}

# A MOSMIX run is valid ~6h (DWD ships 4 runs/day: 03/09/15/21 UTC); mirrors
# WeatherClient.DISK_CACHE_TTL_S, the same cadence reasoning for Open-Meteo.
_CACHE_TTL_S = 21600

_NS = {
    "kml": "http://www.opengis.net/kml/2.2",
    # Underscore, not dot, before the version digit — verified against a live
    # fetch 2026-09-28; a "V1.0.xsd" namespace (an easy typo) silently matches
    # nothing and fetch_station() would return None for every station forever.
    "dwd": "https://opendata.dwd.de/weather/lib/pointforecast_dwd_extension_V1_0.xsd",
}
_DWD_ELEMENT_NAME = f"{{{_NS['dwd']}}}elementName"


@dataclass
class MosmixForecast:
    wmo: str
    issue_time: datetime  # UTC
    times: list[datetime]  # UTC, one per forecast step
    ttt_c: list[float | None]  # 2m temperature, degC (converted from Kelvin)
    e_ttt_c: list[float | None]  # standard error of TTT, degC (a delta — Kelvin == degC, no offset)


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s).astimezone(timezone.utc)
    except ValueError:
        return None


def _parse_values(text: str | None) -> list[float | None]:
    """DWD's dwd:value is whitespace-separated; missing readings are '-'."""
    if not text:
        return []
    out: list[float | None] = []
    for tok in text.split():
        if tok == "-":
            out.append(None)
            continue
        try:
            out.append(float(tok))
        except ValueError:
            out.append(None)
    return out


def _parse_kmz(data: bytes) -> MosmixForecast | None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith(".kml")]
            if not names:
                return None
            xml_bytes = zf.read(names[0])
    except zipfile.BadZipFile:
        return None
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return None

    doc = root.find("kml:Document", _NS)
    if doc is None:
        return None

    issue_el = doc.find(".//dwd:ProductDefinition/dwd:IssueTime", _NS)
    issue_time = _parse_dt(issue_el.text if issue_el is not None else None)

    steps = [s for s in (
        _parse_dt(ts.text) for ts in doc.findall(".//dwd:ForecastTimeSteps/dwd:TimeStep", _NS)
    ) if s is not None]

    placemark = doc.find("kml:Placemark", _NS)
    if placemark is None:
        return None
    name_el = placemark.find("kml:name", _NS)
    wmo = (name_el.text or "").strip() if name_el is not None else ""
    if not wmo or not steps:
        return None

    ttt_c: list[float | None] = []
    e_ttt_c: list[float | None] = []
    for fc in placemark.findall(".//dwd:Forecast", _NS):
        element = fc.get(_DWD_ELEMENT_NAME)
        value_el = fc.find("dwd:value", _NS)
        vals = _parse_values(value_el.text if value_el is not None else None)
        if element == "TTT":
            ttt_c = [(v - 273.15) if v is not None else None for v in vals]
        elif element == "E_TTT":
            e_ttt_c = vals

    if not ttt_c:
        return None

    n = min(len(steps), len(ttt_c))
    times = steps[:n]
    ttt_c = ttt_c[:n]
    if len(e_ttt_c) < n:
        e_ttt_c = e_ttt_c + [None] * (n - len(e_ttt_c))
    e_ttt_c = e_ttt_c[:n]

    return MosmixForecast(
        wmo=wmo, issue_time=issue_time or times[0],
        times=times, ttt_c=ttt_c, e_ttt_c=e_ttt_c,
    )


# ── disk cache (mirrors WeatherClient's ensemble cache — see weather_client.py) ──
def _cache_path(wmo: str):
    from .paths import DATA_DIR
    return DATA_DIR / "cache" / "mosmix" / f"{wmo}.json"


def _cache_get(wmo: str) -> MosmixForecast | None:
    p = _cache_path(wmo)
    try:
        if not p.exists() or time.time() - p.stat().st_mtime > _CACHE_TTL_S:
            return None
        d = json.loads(p.read_text())
        return MosmixForecast(
            wmo=d["wmo"],
            issue_time=datetime.fromisoformat(d["issue_time"]),
            times=[datetime.fromisoformat(t) for t in d["times"]],
            ttt_c=d["ttt_c"],
            e_ttt_c=d["e_ttt_c"],
        )
    except Exception:  # noqa: BLE001 — cache is an optimization, never a blocker
        return None


def _cache_put(fc: MosmixForecast) -> None:
    p = _cache_path(fc.wmo)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "wmo": fc.wmo,
            "issue_time": fc.issue_time.isoformat(),
            "times": [t.isoformat() for t in fc.times],
            "ttt_c": fc.ttt_c,
            "e_ttt_c": fc.e_ttt_c,
        }))
        tmp.replace(p)
    except Exception:  # noqa: BLE001
        pass


def fetch_station(wmo: str, use_cache: bool = True) -> MosmixForecast | None:
    """Latest MOSMIX_L forecast for a WMO station id, or None — unknown id, DWD
    outage, or a malformed payload all stand the feature down rather than raise."""
    wmo = (wmo or "").strip()
    if not wmo:
        return None
    if use_cache:
        cached = _cache_get(wmo)
        if cached is not None:
            return cached
    url = f"{_BASE}/{wmo}/kml/MOSMIX_L_LATEST_{wmo}.kmz"
    try:
        req = urllib.request.Request(url, headers=_UA)
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
    except Exception:  # noqa: BLE001 — no SLA; a miss must not raise
        return None
    fc = _parse_kmz(data)
    if fc is None:
        return None
    _cache_put(fc)
    return fc


def daily_extreme(fc: MosmixForecast, day: date, tz: str, kind: str = "max") -> tuple[float, float] | None:
    """(mean_degC, sigma_degC) for `day`'s station-local-calendar extreme.

    mean is the max/min hourly TTT within the local day; sigma starts as
    max(E_TTT) over those same hours — combining hourly steps isn't
    independent and the max of hourly means understates the true daily
    extreme, so this is a starting point (T1 measures and corrects the
    residual bias), not a calibrated number. Returns None if the station's
    local day isn't covered by this forecast, or E_TTT is unpopulated for it —
    a forecast without honest uncertainty isn't something callers should use
    silently as if it were."""
    try:
        zone = ZoneInfo(tz)
    except Exception:  # noqa: BLE001 — bad/unknown tz string
        return None
    if kind not in ("max", "min"):
        raise ValueError(f"kind must be 'max' or 'min', got {kind!r}")
    reducer = max if kind == "max" else min

    ttt_vals: list[float] = []
    sigma_vals: list[float] = []
    for t, ttt, e_ttt in zip(fc.times, fc.ttt_c, fc.e_ttt_c):
        if t.astimezone(zone).date() != day:
            continue
        if ttt is not None:
            ttt_vals.append(ttt)
        if e_ttt is not None:
            sigma_vals.append(e_ttt)

    if not ttt_vals or not sigma_vals:
        return None
    return reducer(ttt_vals), max(sigma_vals)
