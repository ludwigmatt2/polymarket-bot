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
                self._entries.append({
                    "city":      row["city"],
                    "lat":       float(row["lat"]),
                    "lon":       float(row["lon"]),
                    "mean_bias": float(row["mean_bias_c"]),
                    "n":         int(row["n"]),
                })
        except Exception:
            pass

    def get_offset(self, lat: float, lon: float) -> float:
        """
        Return a confidence-scaled temperature offset in °C for the nearest city.
        Returns 0.0 if none is within 100 km or the nearest has too few samples.

        offset > 0 means model runs cold → we lower the threshold to compensate.
        offset < 0 means model runs warm → we raise the threshold.

        The raw per-city bias is scaled by min(n/FULL_CONFIDENCE_N, 1.0), so a
        well-sampled city applies its full bias while a thin one applies a fraction.
        """
        if not self._entries:
            return 0.0
        best, best_dist = None, float("inf")
        for e in self._entries:
            d = _haversine(lat, lon, e["lat"], e["lon"])
            if d < best_dist:
                best_dist, best = d, e
        if not best or best_dist >= 100:
            return 0.0
        if best["n"] < MIN_BIAS_N:
            return 0.0
        confidence = min(best["n"] / FULL_CONFIDENCE_N, 1.0)
        return best["mean_bias"] * confidence

    def summary(self) -> str:
        if not self._entries:
            return "No city bias corrections loaded."
        parts = []
        for e in self._entries:
            if e["n"] < MIN_BIAS_N:
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
