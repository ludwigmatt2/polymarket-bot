"""
Signal generator — compares model probability to market price and applies quality gates.

A Signal is only produced when all gates pass AND the edge exceeds the fee threshold.
Every evaluation (pass or fail) is logged for diagnostic purposes.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .config import (
    MAX_ENSEMBLE_SPREAD,
    MAX_MARKET_PRICE,
    MIN_EDGE_PP,
    MIN_ENSEMBLE_MEMBERS,
    MIN_MARKET_LIQUIDITY_USD,
    ROUND_TRIP_FEE,
)
from .models import EnsembleForecast, RawProbabilityResult, Signal, WeatherMarket
from .probability_model import ProbabilityModel
from .weather_client import WeatherClient


class SignalGenerator:
    def __init__(self, model: ProbabilityModel, client: WeatherClient):
        self.model = model
        self.client = client

    def evaluate(self, market: WeatherMarket) -> Signal:
        """
        Full pipeline for one market:
        1. Fetch ensemble forecast
        2. Compute calibrated P(outcome)
        3. Apply quality gates
        4. Return Signal (gate_passed=False if rejected, with reason)
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

        gate_passed, rejection_reason = self._quality_gates(market, forecast, prob_result)

        model_p = prob_result.calibrated_p
        market_p = market.yes_price
        edge_pp = abs(model_p - market_p)
        direction = "YES" if model_p > market_p else "NO"

        # Confidence score: 1 at zero spread, 0 at MAX_ENSEMBLE_SPREAD
        spread_norm = min(prob_result.ensemble_spread / MAX_ENSEMBLE_SPREAD, 1.0)
        confidence_score = round(1.0 - spread_norm, 3)

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
    ) -> tuple[bool, str | None]:
        """
        Returns (passes, rejection_reason).
        All gates must pass for a tradeable signal.
        """
        if prob.n_members < MIN_ENSEMBLE_MEMBERS:
            return False, f"insufficient_members:{prob.n_members}"

        if prob.ensemble_spread > MAX_ENSEMBLE_SPREAD:
            return False, f"high_spread:{prob.ensemble_spread:.3f}"

        if market.liquidity_usd < MIN_MARKET_LIQUIDITY_USD:
            return False, f"low_liquidity:{market.liquidity_usd:.0f}"

        if not (0.0 < market.yes_price < 1.0):
            return False, f"invalid_market_price:{market.yes_price}"

        if market.yes_price < (1 - MAX_MARKET_PRICE) or market.yes_price > MAX_MARKET_PRICE:
            return False, f"extreme_price:{market.yes_price:.3f}"

        edge = abs(prob.calibrated_p - market.yes_price)
        if edge < MIN_EDGE_PP:
            return False, f"insufficient_edge:{edge:.3f}_required:{MIN_EDGE_PP}"

        return True, None
