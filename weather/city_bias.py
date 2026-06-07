"""
City-level temperature bias correction.

Reads logs/city_bias.csv (written by city_bias_report.py) and provides
per-city temperature offsets to apply to thresholds before probability
computation.

Usage in SignalGenerator:
    corrector = CityBiasCorrector()
    offset = corrector.get_offset(lat, lon)   # °C
    adjusted_threshold = threshold - offset
"""

from __future__ import annotations

import csv
import math
from pathlib import Path


# Phase 2 confidence scaling: a city's raw bias is trusted in proportion to its
# sample size, reaching full weight at this many observations.
FULL_CONFIDENCE_N = 15
# Below this many observations a single noisy reading can't move a threshold.
MIN_BIAS_N = 3


class CityBiasCorrector:
    def __init__(self, bias_path: Path = Path("logs/city_bias.csv")):
        self._entries: list[dict] = []
        self._load(bias_path)

    def _load(self, path: Path) -> None:
        if not path.exists():
            return
        try:
            for row in csv.DictReader(open(path)):
                # Phase 2: load ALL cities (no reliable filter). Keep the RAW
                # mean_bias_c plus n; confidence scaling happens at read time in
                # get_offset. Do NOT use damped_bias_c — scaling here as well would
                # damp twice (Atlanta would become 0.656×0.33 instead of 2.622×0.33).
                # Phase 3: a `month` column (1–12) keys seasonal cells; month 0 is the
                # all-season fallback. Old CSVs without the column load as month 0,
                # so behaviour is identical to Phase 2.
                self._entries.append({
                    "city":      row["city"],
                    "lat":       float(row["lat"]),
                    "lon":       float(row["lon"]),
                    "month":     int(row.get("month", 0) or 0),
                    "mean_bias": float(row["mean_bias_c"]),
                    "n":         int(row["n"]),
                })
        except Exception:
            pass

    def get_offset(self, lat: float, lon: float, month: int = 0) -> float:
        """
        Return a confidence-scaled temperature offset in °C for the nearest city.
        Returns 0.0 if none is within 100 km or the nearest has too few samples.

        offset > 0 means model runs cold → we lower the threshold to compensate.
        offset < 0 means model runs warm → we raise the threshold.

        Phase 3 fallback chain at the nearest location: exact month → all-season
        (month 0) → 0.0. The raw bias is scaled by min(n/FULL_CONFIDENCE_N, 1.0),
        so a well-sampled cell applies its full bias and a thin one a fraction.
        Calling with month=0 (the default) reproduces the Phase-2 flat behaviour.
        """
        if not self._entries:
            return 0.0
        # Nearest known location (any month), then resolve the month within it.
        best_loc, best_dist = None, float("inf")
        for e in self._entries:
            d = _haversine(lat, lon, e["lat"], e["lon"])
            if d < best_dist:
                best_dist, best_loc = d, (e["lat"], e["lon"])
        if best_loc is None or best_dist >= 100:
            return 0.0
        at_loc = [e for e in self._entries if (e["lat"], e["lon"]) == best_loc]
        for target in (month, 0):
            for e in at_loc:
                if e["month"] == target and e["n"] >= MIN_BIAS_N:
                    return e["mean_bias"] * min(e["n"] / FULL_CONFIDENCE_N, 1.0)
        return 0.0

    def summary(self) -> str:
        if not self._entries:
            return "No city bias corrections loaded."
        parts = []
        for e in self._entries:
            if e["month"] != 0 or e["n"] < MIN_BIAS_N:   # all-season rows only
                continue
            conf = min(e["n"] / FULL_CONFIDENCE_N, 1.0)
            parts.append(f"{e['city']} {e['mean_bias'] * conf:+.2f}°C(n={e['n']})")
        return "  ".join(parts) if parts else "No city bias corrections loaded."


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km."""
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))
