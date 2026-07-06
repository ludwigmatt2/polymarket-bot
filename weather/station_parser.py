"""Parse the resolving airport station from a Polymarket weather market's rules.

Every daily-temperature market's description names the station and links its
Wunderground history page, e.g.:
  "...highest temperature recorded at the Miami Intl Airport Station ...
   https://www.wunderground.com/history/daily/us/fl/miami/KMIA"
The URL is the most reliable signal: its first path segment is the 2-letter
country and the final segment is the 4-letter ICAO — exactly what wu_client needs.
US URLs carry a state segment (us/fl/miami/KMIA); international ones don't
(kr/incheon/RKSI) — the ICAO is always last.
"""

from __future__ import annotations

import re

_WU_URL = re.compile(
    r"wunderground\.com/history/daily/([a-z]{2})/(?:[a-z0-9-]+/)+([a-z]{4})",
    re.IGNORECASE,
)
# Precision the market resolves on, and unit (°F for US markets, °C elsewhere).
_UNIT = re.compile(r"in degrees (Fahrenheit|Celsius)", re.IGNORECASE)


def station_from_description(desc: str | None) -> dict | None:
    """Return {'icao','country','unit'} parsed from a market description, or None.
    `unit` is 'F' or 'C' (the whole-degree unit the market resolves in)."""
    if not desc:
        return None
    m = _WU_URL.search(desc)
    if not m:
        return None
    country, icao = m.group(1).upper(), m.group(2).upper()
    u = _UNIT.search(desc)
    if u:
        unit = "F" if u.group(1).lower().startswith("f") else "C"
    else:
        unit = "F" if country == "US" else "C"
    return {"icao": icao, "country": country, "unit": unit}
