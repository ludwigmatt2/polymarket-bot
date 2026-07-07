"""Weather Underground direct reader — the literal source Polymarket names.

Each temperature market resolves to "the highest temperature recorded at the
{airport} Station ... source: Wunderground". The WU history page is JS-rendered;
its data comes from Weather.com's internal `api.weather.com` endpoint, which we
read with the site's embedded web key (no signup). Verified: reproduced 5/5 of
the Jul-4 US markets vs on-chain settlement — because it *is* the resolving value.

Caveats: unofficial endpoint + embedded key that can rotate (override via the
WU_API_KEY env var). Because money PnL books from on-chain settlement, a WU
outage only degrades the model's labels, never funds — iem_client is the
sanctioned fallback (see weather/station_truth.py).
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from datetime import date

from .iem_client import is_us, station_meta

_HISTORICAL = "https://api.weather.com/v1/location/{icao}:9:{cc}/observations/historical.json"
# WU frontend web key — public, embedded in wunderground.com. Override via env.
_DEFAULT_KEY = "e1f10a1e78da46f5b10a1e78da96f525"
_UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}


def _country(icao: str) -> str | None:
    """2-letter country for the WU location key. US ASOS → 'US'; intl IEM networks
    are '{CC}__ASOS' so the country is the network prefix."""
    m = station_meta(icao)
    if not m:
        return None
    return "US" if is_us(icao) else m["network"][:2]


def daily_high_low(icao: str, day: date, country: str | None = None,
                   units: str = "e") -> dict | None:
    """Daily max/min for the station's local `day` from WU obs, or None on any
    failure. units='e' (English) → {'max_f','min_f','source'} in whole °F;
    units='m' (metric) → {'max_c','min_c','source'} in whole °C — WU's OWN metric
    values, so °C markets never re-convert an already-rounded °F reading (the
    double-rounding trap: 30.4°C → WU 87°F → back-converted 30.56 → rounds to 31,
    while WU's metric page shows 30). `country` (2-letter, e.g. from the market's
    Wunderground URL) lets this work for stations not in the IEM registry; falls
    back to the registry when omitted."""
    cc = (country or "").strip().upper() or _country(icao)
    if not cc:
        return None
    key = os.environ.get("WU_API_KEY", _DEFAULT_KEY)
    url = _HISTORICAL.format(icao=icao.strip().upper(), cc=cc) + "?" + urllib.parse.urlencode({
        "apiKey": key, "units": units,
        "startDate": day.strftime("%Y%m%d"), "endDate": day.strftime("%Y%m%d"),
    })
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=_UA), timeout=20) as r:
            data = json.loads(r.read().decode())
    except Exception:  # noqa: BLE001 — any failure → fall back to IEM
        return None
    obs = data.get("observations") or []
    temps = [o.get("temp") for o in obs if o.get("temp") is not None]
    if not temps:
        return None
    suffix = "c" if units == "m" else "f"
    return {f"max_{suffix}": float(max(temps)), f"min_{suffix}": float(min(temps)),
            "source": "wunderground"}
