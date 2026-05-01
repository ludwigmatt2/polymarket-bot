"""
Open-Meteo ensemble API client.

Fetches multi-model probabilistic forecasts and returns EnsembleForecast objects
with full member arrays. P(outcome) is computed from the fraction of members
satisfying the condition — no assumptions about the distribution shape.
"""

from __future__ import annotations

import calendar
import time
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Any

import requests

from .config import (
    ENSEMBLE_MODELS,
    FORECAST_MODELS,
    OPEN_METEO_ARCHIVE_URL,
    OPEN_METEO_ENSEMBLE_URL,
    OPEN_METEO_FORECAST_URL,
    OPEN_METEO_GEOCODING_URL,
    OPEN_METEO_REQUEST_TIMEOUT,
)
from .models import EnsembleForecast, Location


class WeatherClient:
    """Fetches ensemble weather forecasts from Open-Meteo (free, no API key)."""

    METRIC_DAILY_PARAMS = {
        "temperature_2m_max": "temperature_2m_max",
        "temperature_2m_min": "temperature_2m_min",
        "precipitation_sum": "precipitation_sum",
        "precipitation_hours": "precipitation_hours",
        "windspeed_10m_max": "windspeed_10m_max",
        "snowfall_sum": "snowfall_sum",
    }

    METRIC_UNITS = {
        "temperature_2m_max": "°C",
        "temperature_2m_min": "°C",
        "precipitation_sum": "mm",
        "precipitation_hours": "h",
        "windspeed_10m_max": "km/h",
        "snowfall_sum": "cm",
    }

    def get_monthly_aggregate_forecast(
        self,
        location: Location,
        month_start: date,
        metric: str,
        models: list[str] | None = None,
    ) -> EnsembleForecast:
        """
        Build an ensemble forecast for a monthly aggregate metric (e.g. total May precipitation).

        Strategy:
          1. Fetch 7-day ensemble — sums per member give a 7-day partial total.
          2. Fetch last two years' archive for the same month to compute a
             scaling ratio: full_month_avg / first_7_days_avg.
          3. Multiply each member's 7-day sum by the ratio to project the full month.

        The resulting EnsembleForecast has member arrays representing projected
        monthly totals, not single-day values — the probability model downstream
        remains unchanged.
        """
        models = models or ENSEMBLE_MODELS
        if metric not in self.METRIC_DAILY_PARAMS:
            raise ValueError(f"Unsupported metric '{metric}'")

        # Step 1: fetch 7-day ensemble member arrays
        raw_member_arrays: dict[str, list[list[float]]] = {}  # model → [member_i_daily_values]
        for model in models:
            try:
                daily_members = self._fetch_ensemble_members_range(
                    location, month_start, days=7, metric=metric, model=model
                )
                if daily_members:
                    raw_member_arrays[model] = daily_members
            except Exception:
                continue

        # Step 2: compute historical monthly/7day ratio
        ratio = self._compute_monthly_scaling_ratio(location, month_start, metric)

        # Step 3: project each member's 7-day sum to full month
        member_arrays: dict[str, list[float]] = {}
        for model, daily_by_member in raw_member_arrays.items():
            projected = [sum(member_vals) * ratio for member_vals in daily_by_member]
            if projected:
                member_arrays[model] = projected

        # Cross-model means (use the projected totals for spread)
        model_means: dict[str, float] = {}
        for model, totals in member_arrays.items():
            if totals:
                model_means[model] = sum(totals) / len(totals)

        last_day = calendar.monthrange(month_start.year, month_start.month)[1]
        target_date = date(month_start.year, month_start.month, last_day)

        return EnsembleForecast(
            lat=location.lat,
            lon=location.lon,
            target_date=target_date,
            metric=metric,
            member_arrays=member_arrays,
            model_means=model_means,
            fetched_at=datetime.utcnow(),
            scaling_ratio=ratio,
        )

    def get_ensemble_forecast(
        self,
        location: Location,
        target_date: date,
        metric: str,
        models: list[str] | None = None,
    ) -> EnsembleForecast:
        """
        Fetch full ensemble member arrays for the given location/date/metric.

        Uses /v1/ensemble which returns all ensemble members per model
        (GFS: 31 members, ICON-EPS: 40 members). P(outcome) is computed
        downstream by counting members satisfying the threshold condition.
        """
        models = models or ENSEMBLE_MODELS
        if metric not in self.METRIC_DAILY_PARAMS:
            raise ValueError(f"Unsupported metric '{metric}'. Supported: {list(self.METRIC_DAILY_PARAMS)}")

        member_arrays: dict[str, list[float]] = {}

        for model in models:
            try:
                members = self._fetch_ensemble_members(location, target_date, metric, model)
                if members:
                    member_arrays[model] = members
            except Exception:
                continue  # Degrade gracefully — use remaining models

        # Cross-model means for uncertainty (spread) calculation
        model_means = self._fetch_forecast_means(location, target_date, metric)

        return EnsembleForecast(
            lat=location.lat,
            lon=location.lon,
            target_date=target_date,
            metric=metric,
            member_arrays=member_arrays,
            model_means=model_means,
            fetched_at=datetime.utcnow(),
        )

    def get_historical_actual(
        self,
        location: Location,
        target_date: date,
        metric: str,
    ) -> float | None:
        """
        Fetch the actual observed value from the Open-Meteo archive.
        Used to compute calibration data (model_p vs actual_outcome).
        """
        params = {
            "latitude": location.lat,
            "longitude": location.lon,
            "start_date": target_date.isoformat(),
            "end_date": target_date.isoformat(),
            "daily": self.METRIC_DAILY_PARAMS[metric],
            "timezone": location.timezone,
        }
        try:
            r = requests.get(OPEN_METEO_ARCHIVE_URL, params=params, timeout=OPEN_METEO_REQUEST_TIMEOUT)
            r.raise_for_status()
            data = r.json()
            values = data.get("daily", {}).get(metric, [None])
            return float(values[0]) if values and values[0] is not None else None
        except Exception:
            return None

    def geocode(self, city_name: str) -> Location | None:
        """Convert a city name to lat/lon via Open-Meteo geocoding API."""
        return _geocode_cached(city_name)

    def _fetch_ensemble_members(
        self,
        location: Location,
        target_date: date,
        metric: str,
        model: str,
    ) -> list[float]:
        """
        Fetch all ensemble member values for a single model on a target date.
        Returns a flat list of member values (one float per member).
        """
        days_ahead = (target_date - date.today()).days + 1
        # Ensemble models cap at 7–10 days; clamp to 7 for safety
        forecast_days = max(1, min(days_ahead, 7))
        params = {
            "latitude": location.lat,
            "longitude": location.lon,
            "models": model,
            "daily": metric,
            "forecast_days": forecast_days,
            "timezone": location.timezone,
        }
        r = requests.get(OPEN_METEO_ENSEMBLE_URL, params=params, timeout=OPEN_METEO_REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()

        daily = data.get("daily", {})
        time_index = _find_time_index(daily, target_date)

        members: list[float] = []
        for key, values in daily.items():
            if key.startswith(metric) and "member" in key:
                if time_index is not None and time_index < len(values):
                    v = values[time_index]
                    if v is not None:
                        members.append(float(v))

        return members

    def _fetch_forecast_means(
        self,
        location: Location,
        target_date: date,
        metric: str,
    ) -> dict[str, float]:
        """
        Fetch deterministic forecast from each model separately for spread calculation.
        Returns {model_name: forecast_value}.
        """
        forecast_days = max(1, (target_date - date.today()).days + 1)
        target_str = target_date.isoformat()
        means: dict[str, float] = {}

        for model in FORECAST_MODELS:
            try:
                params = {
                    "latitude": location.lat,
                    "longitude": location.lon,
                    "models": model,
                    "daily": metric,
                    "forecast_days": forecast_days,
                    "timezone": location.timezone,
                }
                r = requests.get(OPEN_METEO_FORECAST_URL, params=params,
                                 timeout=OPEN_METEO_REQUEST_TIMEOUT)
                r.raise_for_status()
                daily = r.json().get("daily", {})
                idx = _find_time_index(daily, target_date)
                if idx is not None:
                    vals = daily.get(metric, [])
                    if idx < len(vals) and vals[idx] is not None:
                        means[model] = float(vals[idx])
            except Exception:
                continue

        return means

    def _fetch_ensemble_members_range(
        self,
        location: Location,
        start_date: date,
        days: int,
        metric: str,
        model: str,
    ) -> list[list[float]]:
        """
        Fetch ensemble member values across a range of days.
        Returns a list of per-member daily value lists:
          [ [day1_val, day2_val, ...], ... ]  — one list per member.
        """
        forecast_days = max(1, min(days, 7))
        params = {
            "latitude": location.lat,
            "longitude": location.lon,
            "models": model,
            "daily": metric,
            "forecast_days": forecast_days,
            "timezone": location.timezone,
        }
        r = requests.get(OPEN_METEO_ENSEMBLE_URL, params=params, timeout=OPEN_METEO_REQUEST_TIMEOUT)
        r.raise_for_status()
        daily = r.json().get("daily", {})
        times = daily.get("time", [])

        date_strs = [(start_date + timedelta(days=i)).isoformat() for i in range(days)]
        indices = [times.index(d) for d in date_strs if d in times]
        if not indices:
            return []

        member_keys = sorted(k for k in daily if k.startswith(metric) and "member" in k)
        if not member_keys:
            return []

        result: list[list[float]] = []
        for key in member_keys:
            vals = daily[key]
            member_days = [float(vals[i]) for i in indices if i < len(vals) and vals[i] is not None]
            if member_days:
                result.append(member_days)
        return result

    def _compute_monthly_scaling_ratio(
        self,
        location: Location,
        month_start: date,
        metric: str,
    ) -> float:
        """
        Estimate full_month_total / first_7_days_total using historical archive data.
        Falls back to 30/7 naive ratio if archive lookup fails.
        """
        last_day = calendar.monthrange(month_start.year, month_start.month)[1]
        naive_ratio = last_day / 7.0

        monthly_totals: list[float] = []
        first7_totals: list[float] = []

        for years_back in (1, 2, 3):
            hist_year = month_start.year - years_back
            hist_start = date(hist_year, month_start.month, 1)
            hist_end_7 = date(hist_year, month_start.month, 7)
            hist_end_month = date(hist_year, month_start.month, last_day)
            try:
                params_full = {
                    "latitude": location.lat, "longitude": location.lon,
                    "start_date": hist_start.isoformat(),
                    "end_date": hist_end_month.isoformat(),
                    "daily": metric, "timezone": location.timezone,
                }
                r = requests.get(OPEN_METEO_ARCHIVE_URL, params=params_full,
                                 timeout=OPEN_METEO_REQUEST_TIMEOUT)
                r.raise_for_status()
                vals = r.json().get("daily", {}).get(metric, [])
                vals_clean = [v for v in vals if v is not None]
                if vals_clean:
                    monthly_totals.append(sum(vals_clean))
                    first7_totals.append(sum(vals_clean[:7]))
            except Exception:
                continue

        if not first7_totals or sum(first7_totals) == 0:
            return naive_ratio

        avg_monthly = sum(monthly_totals) / len(monthly_totals)
        avg_first7 = sum(first7_totals) / len(first7_totals)
        return avg_monthly / avg_first7 if avg_first7 > 0 else naive_ratio


def _find_time_index(daily: dict, target_date: date) -> int | None:
    times = daily.get("time", [])
    target_str = target_date.isoformat()
    return times.index(target_str) if target_str in times else None


@lru_cache(maxsize=256)
def _geocode_cached(city_name: str) -> Location | None:
    """Cached geocoding — avoids repeated API calls for the same city."""
    params = {"name": city_name, "count": 1, "language": "en", "format": "json"}
    try:
        r = requests.get(OPEN_METEO_GEOCODING_URL, params=params, timeout=10)
        r.raise_for_status()
        results = r.json().get("results", [])
        if not results:
            return None
        hit = results[0]
        return Location(
            city=hit.get("name", city_name),
            lat=float(hit["latitude"]),
            lon=float(hit["longitude"]),
            timezone=hit.get("timezone", "auto"),
            country=hit.get("country", ""),
        )
    except Exception:
        return None
