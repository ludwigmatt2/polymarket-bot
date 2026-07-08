"""Discover and verify resolving stations for all live temperature markets.

Sweeps Gamma with the scanner's search terms, parses every temperature market's
rules text for its resolving station (WU/NOAA URL + unit), and for each station
NOT yet in the IEM registry runs the verification battery:

  1. IEM network membership (geojson) → network, sid, coords, timezone
  2. Wunderground probe for yesterday → does the WU truth source serve it?
  3. Unit sanity (explicit rules text vs country fallback)

Prints registry-ready entries for the survivors and a skip report for the rest.
Read-only: registering a station stays a reviewed code change in iem_client.

Run:  venv/bin/python scripts/discover_stations.py
"""

from __future__ import annotations

import json
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from weather import station_parser, wu_client  # noqa: E402
from weather.config import WEATHER_SEARCH_TERMS  # noqa: E402
from weather.iem_client import _STATION_REGISTRY  # noqa: E402

_GAMMA_SEARCH = "https://gamma-api.polymarket.com/public-search"
_IEM_NET = "https://mesonet.agron.iastate.edu/geojson/network/{net}.geojson"
_UA = {"User-Agent": "Mozilla/5.0"}


def _get_json(url: str):
    with urllib.request.urlopen(urllib.request.Request(url, headers=_UA), timeout=20) as r:
        return json.loads(r.read().decode())


def sweep_markets() -> dict[str, dict]:
    """ICAO → {country, unit, cities:set, url_country} for every temp market found."""
    found: dict[str, dict] = {}
    seen_conditions: set[str] = set()
    for term in WEATHER_SEARCH_TERMS:
        # Same endpoint + shape the scanner uses (public-search → events → markets)
        try:
            data = _get_json(f"{_GAMMA_SEARCH}?q={urllib.parse.quote(term)}&limit=50")
        except Exception as exc:  # noqa: BLE001
            print(f"  ! gamma query '{term}' failed: {exc}", file=sys.stderr)
            continue
        time.sleep(0.3)
        for event in data.get("events", []):
            for m in event.get("markets", []):
                cid = m.get("conditionId") or m.get("id")
                if not cid or cid in seen_conditions:
                    continue
                seen_conditions.add(cid)
                title = m.get("question") or event.get("title") or ""
                if "temperature" not in title.lower():
                    continue
                desc = m.get("description") or event.get("description") or ""
                st = station_parser.station_from_description(desc)
                if not st:
                    continue
                rec = found.setdefault(st["icao"], {
                    "country": st["country"], "unit": st["unit"], "cities": set()})
                city = title.split(" in ", 1)[-1].split(" on ", 1)[0] if " in " in title else "?"
                rec["cities"].add(city.strip()[:24])
    return found


def iem_lookup(icao: str, url_country: str) -> dict | None:
    """Find the station in IEM's network geojson. US networks are {STATE}_ASOS —
    the state isn't in the ICAO, so probe the states seen in WU URLs is not
    possible here; instead try the national _ASOS convention via the sid search
    endpoint style: for US we scan the most likely networks by trying the
    two-letter prefixes present in existing registry entries plus a static list."""
    is_us = icao.startswith("K") and len(icao) == 4 and (url_country or "").upper() in ("US", "")
    candidates: list[tuple[str, str]] = []
    if is_us:
        # All US states — one geojson each is too slow; use IEM's station search
        try:
            d = _get_json(f"https://mesonet.agron.iastate.edu/api/1/station/{icao[1:]}.json")
            for e in d.get("data", []):
                net = e.get("network", "")
                if net.endswith("_ASOS"):
                    return {"network": net, "sid": icao[1:], "tz": e.get("tzname"),
                            "lat": round(float(e["latitude"]), 4),
                            "lon": round(float(e["longitude"]), 4),
                            "name": e.get("name", "")}
        except Exception:  # noqa: BLE001
            return None
        return None
    cc = (url_country or "").upper() or {"L": "IL"}.get(icao[0], "")
    if cc == "CA":
        # Canadian IEM networks are province-scoped: CA_{PROV}_ASOS
        for prov in ("ON", "QC", "BC", "AB", "MB", "NS", "SK", "NB", "NL"):
            candidates.append((f"CA_{prov}_ASOS", icao))
    elif cc:
        candidates.append((f"{cc}__ASOS", icao))
    for net, sid in candidates:
        try:
            g = _get_json(_IEM_NET.format(net=net))
        except Exception:  # noqa: BLE001
            continue
        for f in g.get("features", []):
            if f.get("id") == sid:
                lon, lat = f["geometry"]["coordinates"]
                return {"network": net, "sid": sid, "tz": f["properties"].get("tzname"),
                        "lat": round(float(lat), 4), "lon": round(float(lon), 4),
                        "name": f["properties"].get("sname", "")}
    return None


def main() -> None:
    print("Sweeping Gamma for temperature markets...")
    found = sweep_markets()
    print(f"  stations referenced by live markets: {len(found)}\n")

    yesterday = date.today() - timedelta(days=1)
    ready, skipped = [], []
    for icao, rec in sorted(found.items()):
        cities = ",".join(sorted(rec["cities"]))
        if icao in _STATION_REGISTRY:
            print(f"  ✓ {icao} already registered  ({cities})")
            continue
        meta = iem_lookup(icao, rec["country"])
        if not meta or not meta.get("tz"):
            skipped.append((icao, cities, "no IEM network entry"))
            continue
        hl = wu_client.daily_high_low(icao, yesterday, rec["country"] or None)
        time.sleep(0.4)
        if not hl:
            skipped.append((icao, cities, "WU probe failed"))
            continue
        ready.append((icao, meta, rec, cities, hl))

    print("\n══ Registry-ready (verified IEM + WU) ══")
    for icao, meta, rec, cities, hl in ready:
        print(f'    "{icao}": ("{meta["network"]}", "{meta["sid"]}", "{meta["tz"]}", '
              f'{meta["lat"]}, {meta["lon"]}),  '
              f'# {cities} — {meta["name"]} (°{rec["unit"]}; WU max_f {hl.get("max_f")})')
    print("\n══ Skipped ══")
    for icao, cities, why in skipped:
        print(f"    {icao} ({cities}): {why}")


if __name__ == "__main__":
    main()
