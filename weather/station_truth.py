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

_METRIC_KIND = {"temperature_2m_max": "max", "temperature_2m_min": "min"}


def daily_value_f(icao: str, day: date, metric: str) -> tuple[float | None, str | None]:
    """(value_°F, source) for the station's daily max/min, WU → IEM-peak → IEM-DSM.
    (None, None) if the metric is unsupported or every source fails."""
    kind = _METRIC_KIND.get(metric)
    if kind is None:
        return None, None

    hl = wu_client.daily_high_low(icao, day)
    if hl:
        return (hl["max_f"] if kind == "max" else hl["min_f"]), "wunderground"

    peak = iem_client.metar_peak(icao, day, kind)
    if peak is not None:
        return peak, "iem_metar_peak"

    dm = iem_client.daily_maxmin(icao, day)
    if dm:
        v = dm["max_f"] if kind == "max" else dm["min_f"]
        if v is not None:
            return float(v), "iem_dsm"

    return None, None


def daily_value_c(icao: str, day: date, metric: str) -> tuple[float | None, str | None]:
    """Same as daily_value_f but the value is converted to °C."""
    v, src = daily_value_f(icao, day, metric)
    return (iem_client.f_to_c(v) if v is not None else None), src
