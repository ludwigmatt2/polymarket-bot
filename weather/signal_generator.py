"""
Signal generator — compares model probability to market price and applies quality gates.

Gate structure (evaluated in order):
  Gate 0:   Forecast freshness   — is Open-Meteo data recent enough?
  Gate 1:   Entry timing window  — not too early (>5d) or too late (<4h)?
  Gate 2.5: Min ensemble members — enough model runs for statistical validity?
  Gate 2.7: Volatility regime    — is cross-model spread acceptably low?
  Gate 5:   Liquidity            — enough USD in the book to enter/exit?
  Gate 7:   Valid price range    — not at degenerate extremes?
  Gate 4:   Fee-adjusted edge    — positive EV after round-trip fees? (blocks most trades)
  Gate 6:   Odds velocity        — fast price movement = informed flow present?
  Gate 8:   Composite confidence — weighted spread + timing + calibration score?

A Signal is produced for every evaluation. quality_gate_passed=False signals carry
a rejection_reason string naming the gate that blocked them.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .config import (
    GATE8_CALIB_WEIGHT,
    GATE8_SPREAD_WEIGHT,
    GATE8_TIMING_WEIGHT,
    MAX_ENTRY_DAYS_AHEAD,
    MAX_ENSEMBLE_SPREAD,
    MAX_FORECAST_AGE_HOURS,
    MAX_MARKET_PRICE,
    MAX_PRICE_VELOCITY_PP,
    MIN_COMPOSITE_CONFIDENCE,
    MIN_ENSEMBLE_MEMBERS,
    MIN_ENTRY_HOURS_AHEAD,
    MIN_MARKET_LIQUIDITY_USD,
    MIN_NET_EV_PP,
    ROUND_TRIP_FEE,
    VELOCITY_WINDOW_HOURS,
)
from .models import EnsembleForecast, RawProbabilityResult, Signal, WeatherMarket
from .probability_model import ProbabilityModel
from .weather_client import WeatherClient


class SignalGenerator:
    def __init__(
        self,
        model: ProbabilityModel,
        client: WeatherClient,
        price_tracker=None,
    ):
        self.model = model
        self.client = client
        self.price_tracker = price_tracker  # Optional PriceTracker for Gate 6

    def evaluate(self, market: WeatherMarket) -> Signal:
        """
        Full pipeline for one market:
        1. Fetch ensemble forecast
        2. Compute calibrated P(outcome)
        3. Apply quality gates (0 → 8)
        4. Record current price in history (for next cycle's Gate 6)
        5. Return Signal
        """
        if market.forecast_start_date is not None:
            forecast = self.client.get_monthly_aggregate_forecast(
                location=market.location,
                month_start=market.forecast_start_date,
                metric=market.metric,
            )
        else:
            forecast = self.client.get_ensemble_forecast(
                location=market.location,
                target_date=market.resolution_date.date(),
                metric=market.metric,
            )

        prob_result = self.model.compute_probability(
            forecast=forecast,
            threshold=market.threshold,
            direction=market.direction,
            threshold_high=market.threshold_high,
        )

        gate_passed, rejection_reason, confidence_score = self._quality_gates(
            market, forecast, prob_result
        )

        # Record price AFTER gate check so Gate 6 velocity uses previous prices
        if self.price_tracker is not None:
            self.price_tracker.record(market.market_id, market.yes_price)

        model_p = prob_result.calibrated_p
        market_p = market.yes_price
        edge_pp = abs(model_p - market_p)
        direction = "YES" if model_p > market_p else "NO"

        return Signal(
            market=market,
            model_p=model_p,
            market_p=market_p,
            edge_pp=round(edge_pp, 4),
            direction=direction,
            ensemble_spread=prob_result.ensemble_spread,
            confidence_score=confidence_score,
            quality_gate_passed=gate_passed,
            rejection_reason=rejection_reason,
            signal_time=datetime.now(timezone.utc),
            forecast=forecast,
            prob_result=prob_result,
        )

    def _quality_gates(
        self,
        market: WeatherMarket,
        forecast: EnsembleForecast,
        prob: RawProbabilityResult,
    ) -> tuple[bool, str | None, float]:
        """
        Returns (passes, rejection_reason, composite_confidence).
        Composite confidence is 0.0 for any early-rejection gate.
        """
        now = datetime.now(timezone.utc)
        days_to_res = (market.resolution_date - now).total_seconds() / 86400

        # Gate 0: Forecast freshness
        fetched_at = forecast.fetched_at
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        age_hours = (now - fetched_at).total_seconds() / 3600
        if age_hours > MAX_FORECAST_AGE_HOURS:
            return False, f"gate0_stale_forecast:{age_hours:.1f}h", 0.0

        # Gate 1: Entry timing window
        if days_to_res > MAX_ENTRY_DAYS_AHEAD:
            return False, f"gate1_too_early:{days_to_res:.1f}d", 0.0
        if days_to_res < MIN_ENTRY_HOURS_AHEAD / 24:
            return False, f"gate1_too_late:{days_to_res * 24:.1f}h", 0.0

        # Gate 2.5: Min ensemble members
        if prob.n_members < MIN_ENSEMBLE_MEMBERS:
            return False, f"gate2.5_insufficient_members:{prob.n_members}", 0.0

        # Gate 2.7: Volatility regime (ensemble spread)
        if prob.ensemble_spread > MAX_ENSEMBLE_SPREAD:
            return False, f"gate2.7_high_spread:{prob.ensemble_spread:.3f}", 0.0

        # Gate 5: Liquidity
        if market.liquidity_usd < MIN_MARKET_LIQUIDITY_USD:
            return False, f"gate5_low_liquidity:{market.liquidity_usd:.0f}", 0.0

        # Gate 7: Valid price range
        if not (0.0 < market.yes_price < 1.0):
            return False, f"gate7_invalid_price:{market.yes_price}", 0.0
        if market.yes_price < (1 - MAX_MARKET_PRICE) or market.yes_price > MAX_MARKET_PRICE:
            return False, f"gate7_extreme_price:{market.yes_price:.3f}", 0.0

        # Gate 4: Fee-adjusted edge (blocks the majority of candidate trades)
        gross_ev = abs(prob.calibrated_p - market.yes_price)
        net_ev = gross_ev - ROUND_TRIP_FEE
        if net_ev < MIN_NET_EV_PP:
            return False, f"gate4_fee_adjusted_edge:{net_ev:.3f}_required:{MIN_NET_EV_PP}", 0.0

        # Gate 6: Odds velocity — fast movement signals informed flow
        if self.price_tracker is not None:
            velocity = self.price_tracker.get_velocity(market.market_id, VELOCITY_WINDOW_HOURS)
            if velocity is not None and abs(velocity) > MAX_PRICE_VELOCITY_PP:
                return False, f"gate6_informed_flow:{velocity:+.3f}", 0.0

        # Gate 8: Composite confidence
        spread_component = 1.0 - min(prob.ensemble_spread / MAX_ENSEMBLE_SPREAD, 1.0)
        timing_component = 1.0 - min(max(days_to_res, 0) / MAX_ENTRY_DAYS_AHEAD, 1.0)
        calib_component = min(
            self.model.n_calibration_obs / self.model.MIN_CALIBRATION_OBS, 1.0
        )
        composite = round(
            GATE8_SPREAD_WEIGHT * spread_component
            + GATE8_TIMING_WEIGHT * timing_component
            + GATE8_CALIB_WEIGHT * calib_component,
            3,
        )
        if composite < MIN_COMPOSITE_CONFIDENCE:
            return False, f"gate8_low_confidence:{composite:.3f}_required:{MIN_COMPOSITE_CONFIDENCE}", composite

        return True, None, composite
