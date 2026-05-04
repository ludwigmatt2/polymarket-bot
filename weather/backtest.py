"""
Backtest harness for the weather prediction model.

For each (city, month, test_year) test case we compare two predictors against
the archived actual outcome:

  1. Climatology — empirical CDF from past years' monthly totals
  2. Model       — current bot logic: project monthly total = first_7_days × ratio,
                   scored as a Gaussian centered at the projection with sigma
                   estimated from train-year residuals.

Both are scored with Brier on a binary "actual exceeded threshold" outcome.

Train years are always the years strictly prior to test_year, so no leakage.
"""

from __future__ import annotations

import calendar
import math
from dataclasses import dataclass, field
from datetime import date

from .weather_client import WeatherClient
from .models import Location


@dataclass
class BacktestCase:
    city: str
    metric: str          # e.g. "precipitation_sum"
    month: int           # 1..12
    test_year: int
    train_years: list[int]
    threshold: float | None = None   # None ⇒ use train-year median (balanced test)


@dataclass
class BacktestResult:
    case: BacktestCase
    actual_total: float
    actual_binary: int            # 1 if actual_total > threshold_used else 0
    threshold_used: float         # resolved threshold (case.threshold or train median)
    p_climatology: float          # P(total > threshold) from train-year empirical
    p_model_old: float            # Old: Gaussian over (first_7 × historical_ratio)
    p_model_blend: float          # New: empirical over (first_7 + historical_remainder[y])
    model_point_pred: float       # first_7_days × scaling_ratio (old)
    train_n: int
    sigma: float                  # std of train-year residuals (old model)
    brier_climatology: float
    brier_model_old: float
    brier_model_blend: float
    notes: str = ""


def _month_bounds(year: int, month: int) -> tuple[date, date]:
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last_day)


def _full_month_total(client: WeatherClient, loc: Location, year: int, month: int, metric: str) -> float | None:
    start, end = _month_bounds(year, month)
    vals = client.get_archive_daily_values(loc, start, end, metric)
    return sum(vals) if vals else None


def _first_n_days_sum(client: WeatherClient, loc: Location, year: int, month: int, days: int, metric: str) -> float | None:
    start = date(year, month, 1)
    end = date(year, month, days)
    vals = client.get_archive_daily_values(loc, start, end, metric)
    return sum(vals) if len(vals) == days else None


def _stdev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mu = sum(values) / len(values)
    return math.sqrt(sum((v - mu) ** 2 for v in values) / (len(values) - 1))


def _normal_cdf(x: float, mu: float, sigma: float) -> float:
    """Standard normal CDF without scipy dependency."""
    if sigma <= 0:
        return 0.0 if x > mu else 1.0
    return 0.5 * (1.0 + math.erf((x - mu) / (sigma * math.sqrt(2.0))))


class Backtester:
    def __init__(self, client: WeatherClient, first_n_days: int = 7):
        self.client = client
        self.first_n_days = first_n_days

    def run_case(self, case: BacktestCase) -> BacktestResult | None:
        """Run a single backtest case. Returns None if data is missing."""
        loc = self.client.geocode(case.city)
        if loc is None:
            return None

        actual_total = _full_month_total(self.client, loc, case.test_year, case.month, case.metric)
        if actual_total is None:
            return None

        train_full: dict[int, float] = {}
        train_first_n: dict[int, float] = {}
        for y in case.train_years:
            full = _full_month_total(self.client, loc, y, case.month, case.metric)
            first = _first_n_days_sum(self.client, loc, y, case.month, self.first_n_days, case.metric)
            if full is not None and first is not None and first > 0:
                train_full[y] = full
                train_first_n[y] = first
        if len(train_full) < 3:
            return None

        # If threshold is None, use the train-year median so each test is balanced
        # (P_climatology ≈ 0.5; the model has to beat coin-flip Brier of 0.25 to add value).
        if case.threshold is None:
            sorted_train = sorted(train_full.values())
            mid = len(sorted_train) // 2
            threshold = (sorted_train[mid] + sorted_train[~mid]) / 2.0
        else:
            threshold = case.threshold
        actual_binary = 1 if actual_total > threshold else 0

        # Climatology — empirical fraction of training years where total > threshold
        p_clim = sum(1 for v in train_full.values() if v > threshold) / len(train_full)

        test_first_n = _first_n_days_sum(self.client, loc, case.test_year, case.month, self.first_n_days, case.metric)
        if test_first_n is None:
            return None

        # OLD model: scale by historical ratio, score via Gaussian residuals.
        ratio = sum(train_full.values()) / sum(train_first_n.values())
        point_pred = test_first_n * ratio
        residuals = [train_full[y] - train_first_n[y] * ratio for y in train_full]
        sigma = _stdev(residuals)
        p_model_old = 1.0 - _normal_cdf(threshold, point_pred, sigma)

        # NEW model: forecast(days 1-7) + historical_remainder(days 8-end) per year.
        # The pseudo-ensemble has one member per train year.
        train_remainders = [train_full[y] - train_first_n[y] for y in train_full]
        ensemble_blend = [test_first_n + r for r in train_remainders]
        p_model_blend = sum(1 for v in ensemble_blend if v > threshold) / len(ensemble_blend)

        return BacktestResult(
            case=case,
            actual_total=actual_total,
            actual_binary=actual_binary,
            threshold_used=threshold,
            p_climatology=p_clim,
            p_model_old=p_model_old,
            p_model_blend=p_model_blend,
            model_point_pred=point_pred,
            train_n=len(train_full),
            sigma=sigma,
            brier_climatology=(p_clim - actual_binary) ** 2,
            brier_model_old=(p_model_old - actual_binary) ** 2,
            brier_model_blend=(p_model_blend - actual_binary) ** 2,
        )

    def run_suite(self, cases: list[BacktestCase]) -> list[BacktestResult]:
        results = []
        for c in cases:
            r = self.run_case(c)
            if r is not None:
                results.append(r)
        return results


def summarize(results: list[BacktestResult]) -> dict:
    """Aggregate Brier and skill metrics across all results."""
    if not results:
        return {"n": 0}
    n = len(results)
    mean_b_clim = sum(r.brier_climatology for r in results) / n
    mean_b_old = sum(r.brier_model_old for r in results) / n
    mean_b_blend = sum(r.brier_model_blend for r in results) / n
    skill_old = 1.0 - (mean_b_old / mean_b_clim) if mean_b_clim > 0 else 0.0
    skill_blend = 1.0 - (mean_b_blend / mean_b_clim) if mean_b_clim > 0 else 0.0
    return {
        "n": n,
        "mean_brier_climatology": mean_b_clim,
        "mean_brier_model_old": mean_b_old,
        "mean_brier_model_blend": mean_b_blend,
        "bss_old": skill_old,
        "bss_blend": skill_blend,
        "old_beats_clim": mean_b_old < mean_b_clim,
        "blend_beats_clim": mean_b_blend < mean_b_clim,
    }


def default_test_suite() -> list[BacktestCase]:
    """Cities matching live paper trades; recent years for which archive data exists."""
    cities = ["Seoul", "Hong Kong", "New York", "London"]
    test_years = [2023, 2024, 2025]
    months = [1, 4, 7, 10]  # one month per quarter for seasonal coverage

    # Threshold per (city, month) chosen at fixed values that approximate
    # historical median monthly precipitation. Will be overwritten with
    # train-year median in practice — keeping reasonable defaults.
    default_threshold_mm = 80.0

    cases = []
    for city in cities:
        for ty in test_years:
            train_years = [y for y in range(ty - 7, ty) if y >= 2015]
            if len(train_years) < 4:
                continue
            for m in months:
                cases.append(BacktestCase(
                    city=city,
                    metric="precipitation_sum",
                    month=m,
                    test_year=ty,
                    train_years=train_years,
                    threshold=default_threshold_mm,
                ))
    return cases
