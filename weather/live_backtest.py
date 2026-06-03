"""
LiveSignalBacktester — replay resolved paper trades through the live signal path.

For each resolved paper trade:
1. Reconstruct WeatherMarket from stored CSV fields.
2. Build a synthetic EnsembleForecast using archive data from the resolution_date
   (same location, same metric, same date).  Multiple "model" pseudo-members are
   constructed from ±7-day historical archive windows in prior years — the same
   approach used by TempBacktester — so the backtester exercises the full gate
   stack without requiring historical ensemble API data.
3. Call SignalGenerator.evaluate(market) with the synthetic forecast injected.
4. Compare replayed direction / edge_pp to originals (parity check).
5. Score replayed model_p vs actual outcome (Brier delta).

Wire via: python weather_bot.py --mode backtest-live
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .models import EnsembleForecast, Location, WeatherMarket
from .probability_model import ProbabilityModel
from .signal_generator import SignalGenerator
from .weather_client import WeatherClient

_log = logging.getLogger(__name__)


@dataclass
class LiveBacktestResult:
    trade_id: str
    signal_time: str
    market_title: str
    original_direction: str        # YES/NO stored in paper trade
    replayed_direction: str        # YES/NO from replay
    direction_match: bool
    original_model_p: float
    replayed_model_p: float
    actual_outcome: int            # 0 or 1
    brier_original: float
    brier_replayed: float
    gate_passed: bool
    rejection_reason: str | None
    n_archive_members: int         # members used in synthetic forecast


@dataclass
class LiveBacktestReport:
    n_total: int                   # resolved trades attempted
    n_replayed: int                # trades where archive fetch succeeded
    n_direction_match: int
    parity_pct: float              # n_direction_match / n_replayed (or 0)
    mean_brier_original: float
    mean_brier_replayed: float
    mean_brier_delta: float        # replayed - original (negative = replayed is better)
    results: list[LiveBacktestResult] = field(default_factory=list)


def _brier(model_p: float, outcome: int) -> float:
    return (model_p - float(outcome)) ** 2


class LiveSignalBacktester:
    """Replay resolved paper trades through the live SignalGenerator."""

    # Number of prior years to pull archive data from for the synthetic ensemble
    ARCHIVE_YEARS_BACK = 3
    # Days window around the resolution day-of-year used for archive members
    ARCHIVE_WINDOW_DAYS = 7
    # Minimum archive members needed to attempt a replay
    MIN_ARCHIVE_MEMBERS = 3

    def __init__(self, model: ProbabilityModel, client: WeatherClient):
        self.model = model
        self.client = client
        self._generator = SignalGenerator(model=model, client=client)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def replay_trades(self, trades_csv: Path) -> LiveBacktestReport:
        """Run backtest over all resolved trades in trades_csv."""
        rows = self._load_resolved(trades_csv)
        results: list[LiveBacktestResult] = []

        for row in rows:
            result = self._replay_row(row)
            if result is not None:
                results.append(result)

        return self._summarise(len(rows), results)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_resolved(self, trades_csv: Path) -> list[dict]:
        if not trades_csv.exists():
            return []
        with trades_csv.open() as f:
            rows = list(csv.DictReader(f))
        return [r for r in rows if r.get("actual_outcome") in ("0", "1")]

    def _replay_row(self, row: dict) -> LiveBacktestResult | None:
        """Try to replay one trade row.  Returns None if archive fetch fails."""
        try:
            lat = float(row["lat"])
            lon = float(row["lon"])
            loc_tz = row.get("location_tz") or "UTC"
            threshold = float(row["threshold"])
            threshold_high = float(row["threshold_high"]) if row.get("threshold_high") else None
            metric = row["metric"]
            weather_direction = row.get("weather_direction", "above")
            res_date = datetime.fromisoformat(row["resolution_date"])
            resolution_date_d = res_date.date() if isinstance(res_date, datetime) else res_date
        except (KeyError, ValueError):
            return None

        loc = Location(city="", lat=lat, lon=lon, timezone=loc_tz)
        archive_members = self._build_archive_members(loc, resolution_date_d, metric)
        if len(archive_members) < self.MIN_ARCHIVE_MEMBERS:
            return None

        market = self._reconstruct_market(row, loc, threshold, threshold_high,
                                          metric, weather_direction, res_date)
        forecast = self._build_forecast(lat, lon, resolution_date_d, metric, archive_members)
        signal = self._evaluate_with_forecast(market, forecast)

        actual = int(row["actual_outcome"])
        orig_model_p = float(row["model_p"])
        orig_direction = row["direction"]

        return LiveBacktestResult(
            trade_id=row.get("trade_id", ""),
            signal_time=row.get("signal_time", ""),
            market_title=row.get("market_title", "")[:60],
            original_direction=orig_direction,
            replayed_direction=signal.direction,
            direction_match=(signal.direction == orig_direction),
            original_model_p=orig_model_p,
            replayed_model_p=round(signal.model_p, 4),
            actual_outcome=actual,
            brier_original=round(_brier(orig_model_p, actual), 4),
            brier_replayed=round(_brier(signal.model_p, actual), 4),
            gate_passed=signal.quality_gate_passed,
            rejection_reason=signal.rejection_reason,
            n_archive_members=len(archive_members),
        )

    def _build_archive_members(
        self,
        location: Location,
        target_date: date,
        metric: str,
    ) -> list[float]:
        """
        Fetch historical archive values for the same calendar-day window
        across prior years to form a pseudo-ensemble.
        """
        members: list[float] = []
        today = date.today()
        for years_back in range(1, self.ARCHIVE_YEARS_BACK + 1):
            year = target_date.year - years_back
            if year < 2020:
                continue
            start = date(year, target_date.month, 1)
            # Build a ±WINDOW window around the same day-of-month
            day_off = target_date.day - 1
            window_start = date(year, target_date.month, max(1, target_date.day - self.ARCHIVE_WINDOW_DAYS))
            import calendar as _cal
            last_dom = _cal.monthrange(year, target_date.month)[1]
            window_end = date(year, target_date.month, min(last_dom, target_date.day + self.ARCHIVE_WINDOW_DAYS))
            if window_end >= today:
                window_end = today - timedelta(days=1)
            if window_end < window_start:
                continue
            try:
                vals = self.client.get_archive_daily_values(location, window_start, window_end, metric)
                members.extend(v for v in vals if v is not None)
            except Exception:
                continue
        return members

    def _reconstruct_market(
        self,
        row: dict,
        loc: Location,
        threshold: float,
        threshold_high: float | None,
        metric: str,
        weather_direction: str,
        resolution_date: datetime,
    ) -> WeatherMarket:
        yes_price = float(row.get("entry_price", 0.5))
        if not resolution_date.tzinfo:
            resolution_date = resolution_date.replace(tzinfo=timezone.utc)
        return WeatherMarket(
            market_id=row.get("market_id", "replay"),
            title=row.get("market_title", ""),
            yes_price=yes_price,
            liquidity_usd=1_000_000.0,  # bypass liquidity gate in backtest
            resolution_date=resolution_date,
            resolution_source="archive",
            location=loc,
            metric=metric,
            threshold=threshold,
            threshold_high=threshold_high,
            direction=weather_direction,
            url="",
        )

    def _build_forecast(
        self,
        lat: float,
        lon: float,
        target_date: date,
        metric: str,
        members: list[float],
    ) -> EnsembleForecast:
        """
        Build a synthetic EnsembleForecast from archive members.
        Split members across three fake model keys so gate 2.6 sees n_models=3.
        """
        n = len(members)
        third = max(1, n // 3)
        member_arrays = {
            "archive_gfs": members[:third],
            "archive_icon": members[third:2 * third],
            "archive_ecmwf": members[2 * third:],
        }
        # Remove empty buckets
        member_arrays = {k: v for k, v in member_arrays.items() if v}
        return EnsembleForecast(
            lat=lat,
            lon=lon,
            target_date=target_date,
            metric=metric,
            member_arrays=member_arrays,
            model_means={k: sum(v) / len(v) for k, v in member_arrays.items()},
            fetched_at=datetime.now(timezone.utc),
        )

    def _evaluate_with_forecast(self, market: WeatherMarket, forecast: EnsembleForecast):
        """Run generator.evaluate but inject a pre-built forecast."""
        orig_fn = self.client.get_ensemble_forecast
        self.client.get_ensemble_forecast = lambda *a, **kw: forecast
        try:
            return self._generator.evaluate(market)
        finally:
            self.client.get_ensemble_forecast = orig_fn

    def _summarise(self, n_total: int, results: list[LiveBacktestResult]) -> LiveBacktestReport:
        n_replayed = len(results)
        if n_replayed == 0:
            return LiveBacktestReport(
                n_total=n_total, n_replayed=0, n_direction_match=0,
                parity_pct=0.0,
                mean_brier_original=0.0, mean_brier_replayed=0.0,
                mean_brier_delta=0.0, results=results,
            )
        n_match = sum(1 for r in results if r.direction_match)
        brier_orig = sum(r.brier_original for r in results) / n_replayed
        brier_rep = sum(r.brier_replayed for r in results) / n_replayed
        return LiveBacktestReport(
            n_total=n_total,
            n_replayed=n_replayed,
            n_direction_match=n_match,
            parity_pct=round(n_match / n_replayed, 3),
            mean_brier_original=round(brier_orig, 4),
            mean_brier_replayed=round(brier_rep, 4),
            mean_brier_delta=round(brier_rep - brier_orig, 4),
            results=results,
        )
