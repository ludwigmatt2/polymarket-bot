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


class CityBiasCorrector:
    def __init__(self, bias_path: Path = Path("logs/city_bias.csv")):
        self._entries: list[dict] = []
        self._load(bias_path)

    def _load(self, path: Path) -> None:
        if not path.exists():
            return
        try:
            for row in csv.DictReader(open(path)):
                if int(row.get("reliable", 0)):
                    self._entries.append({
                        "city":   row["city"],
                        "lat":    float(row["lat"]),
                        "lon":    float(row["lon"]),
                        "offset": float(row["damped_bias_c"]),
                    })
        except Exception:
            pass

    def get_offset(self, lat: float, lon: float) -> float:
        """
        Return temperature offset in °C for the nearest known city.
        Returns 0.0 if no reliable entry is close enough.

        offset > 0 means model runs cold → we lower the threshold to compensate.
        offset < 0 means model runs warm → we raise the threshold.
        """
        if not self._entries:
            return 0.0
        best, best_dist = None, float("inf")
        for e in self._entries:
            d = _haversine(lat, lon, e["lat"], e["lon"])
            if d < best_dist:
                best_dist, best = d, e
        # Only apply if within 100 km of a known city
        return best["offset"] if best and best_dist < 100 else 0.0

    def summary(self) -> str:
        if not self._entries:
            return "No city bias corrections loaded."
        return "  ".join(f"{e['city']} {e['offset']:+.2f}°C" for e in self._entries)


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km."""
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))
