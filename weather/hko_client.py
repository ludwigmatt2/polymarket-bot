"""Hong Kong Observatory direct reader — the literal source HK markets resolve on.

HK temperature markets resolve to the "Absolute Daily Max/Min (deg. C)" from the
HKO's own "Daily Extract" (https://www.weather.gov.hk/en/cis/climat.htm), NOT the
VHHH airport METAR already in the IEM registry — those are two different physical
stations (HKO's reference site is the urban Tsim Sha Tsui observatory, ~10km from
Chek Lap Kok airport). Routing HK markets through VHHH would repeat the Dallas
KDAL-vs-KDFW mistake: a registered-but-wrong station.

CRITICAL CAVEAT — verified live (Jul 2026): the HKO Open Data CIS endpoint only
serves FINALIZED months. Querying the current or immediately preceding month
returns an empty/header-only response; the market's own rules text confirms this
("This market can not resolve until data for this date has been published").
`daily_high_low` therefore returns None for any recent date — this is truth-only
plumbing for backchecking/calibration once HKO finalizes the month, NOT a live
intraday feed. Trading dispatch is intentionally NOT wired to this client yet;
see weather/station_truth.py docstring.
"""

from __future__ import annotations

import csv
import io
import urllib.parse
import urllib.request
from datetime import date

_OPENDATA = "https://data.weather.gov.hk/weatherAPI/opendata/opendata.php"
_UA = {"User-Agent": "Mozilla/5.0"}
STATION = "HKO"  # HKO's own station code for the CIS API (not an ICAO)


def _fetch_month(data_type: str, year: int, month: int) -> dict[int, float]:
    """day -> value(°C) for a finalized month, or {} if unavailable/incomplete."""
    params = {"dataType": data_type, "station": STATION, "year": year,
              "month": month, "lang": "en"}
    url = f"{_OPENDATA}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, headers=_UA), timeout=20
        ) as r:
            body = r.read().decode("utf-8-sig")
    except Exception:  # noqa: BLE001
        return {}
    out: dict[int, float] = {}
    for row in csv.reader(io.StringIO(body)):
        if len(row) < 4 or not row[0].isdigit():
            continue
        try:
            out[int(row[2])] = float(row[3])
        except ValueError:
            continue
    return out


def daily_high_low(day: date) -> dict | None:
    """{'max_c','min_c','source'} for HKO's finalized daily extract, or None if
    the month isn't finalized yet (recent dates) or the day has no entry."""
    highs = _fetch_month("CLMMAXT", day.year, day.month)
    lows = _fetch_month("CLMMINT", day.year, day.month)
    mx, mn = highs.get(day.day), lows.get(day.day)
    if mx is None and mn is None:
        return None
    return {"max_c": mx, "min_c": mn, "source": "hko_cis"}
