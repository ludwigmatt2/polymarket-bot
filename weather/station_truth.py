"""Unified station daily-value truth for a resolving airport station.

Source precedence (each covers the previous one's weakness):
  1. Wunderground-direct  — the exact value Polymarket resolves on (5/5 vs on-chain)
  2. IEM raw-METAR peak   — sanctioned free proxy; matched WU on the edge cases
  3. IEM DSM daily        — last resort (2-min-avg, can sit ±1°F from Wunderground)

Returns the station's daily max/min in °F plus which source answered, so callers
can log provenance and flag when they fell back off Wunderground. Money PnL still
books from on-chain settlement — this is the *model's* label truth, not the ledger.
"""

from __future__ import annotations

from datetime import date

from . import iem_client, wu_client
from .models import _evaluate_outcome

_METRIC_KIND = {"temperature_2m_max": "max", "temperature_2m_min": "min"}


def _fetch_day(icao: str, day: date, country: str | None) -> dict | None:
    """One probe covering BOTH metrics: {'max_f','min_f','source'} or None. WU and
    IEM DSM both return max+min in a single call; metar_peak's two reductions run off
    the same obs set (both present or both absent). So one call serves high AND low
    markets for a station/day."""
    hl = wu_client.daily_high_low(icao, day, country)
    if hl:
        return {"max_f": hl["max_f"], "min_f": hl["min_f"], "source": "wunderground"}
    mx = iem_client.metar_peak(icao, day, "max")
    mn = iem_client.metar_peak(icao, day, "min")
    if mx is not None or mn is not None:
        return {"max_f": mx, "min_f": mn, "source": "iem_metar_peak"}
    dm = iem_client.daily_maxmin(icao, day)
    if dm and (dm.get("max_f") is not None or dm.get("min_f") is not None):
        return {"max_f": dm.get("max_f"), "min_f": dm.get("min_f"), "source": "iem_dsm"}
    return None


def daily_value_f(icao: str, day: date, metric: str, country: str | None = None,
                  cache: dict | None = None) -> tuple[float | None, str | None]:
    """(value_°F, source) for the station's daily max/min, WU → IEM-peak → IEM-DSM.
    `country` (from the market's Wunderground URL) lets WU resolve stations outside
    the seeded registry. Pass a `cache` dict to memoize the day's fetch across trades
    that share a station/day within one resolve pass — high and low markets then
    reuse one probe. (None, None) if unsupported or every source fails."""
    kind = _METRIC_KIND.get(metric)
    if kind is None:
        return None, None
    key = (icao, day.isoformat())
    if cache is not None and key in cache:
        rec = cache[key]
    else:
        rec = _fetch_day(icao, day, country)
        if cache is not None:
            cache[key] = rec
    if not rec:
        return None, None
    v = rec["max_f"] if kind == "max" else rec["min_f"]
    return (float(v), rec["source"]) if v is not None else (None, None)


def station_outcome(icao: str, country: str, unit: str, day: date, metric: str,
                    threshold_c: float, threshold_high_c: float | None, direction: str,
                    cache: dict | None = None) -> tuple[bool | None, str | None, float | None]:
    """Resolve a market's YES/NO outcome the way Polymarket does: take the station's
    daily max/min, round to whole degrees IN THE MARKET'S UNIT, then apply the bucket
    rule (shared with the Open-Meteo resolver via _evaluate_outcome). Thresholds are
    stored in °C; for °F markets they are exact °F edges, so we compare in whole °F.
    Returns (yes_condition | None, source, rounded_station_value_in_market_unit)."""
    v_f, src = daily_value_f(icao, day, metric, country, cache)
    if v_f is None:
        return None, None, None

    if (unit or "").upper() == "F":
        val = round(v_f)
        lo = round(iem_client.c_to_f(threshold_c))
        hi = round(iem_client.c_to_f(threshold_high_c)) if threshold_high_c is not None else None
    else:  # °C market
        val = round(iem_client.f_to_c(v_f))
        lo = round(threshold_c)
        hi = round(threshold_high_c) if threshold_high_c is not None else None

    return _evaluate_outcome(val, lo, direction, hi), src, float(val)
