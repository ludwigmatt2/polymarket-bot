"""
Signal generator — compares model probability to market price and applies quality gates.

Gate structure (evaluated in order):
  Gate 0:   Forecast freshness   — is Open-Meteo data recent enough?
  Gate 1:   Entry timing window  — not too early (>5d) or too late (<4h)?
  Gate 2.5: Min ensemble members — enough model runs for statistical validity?
  Gate 2.6: Model diversity      — at least 2 distinct models required?
  Gate 2.7: Volatility regime    — is cross-model spread acceptably low?
  Gate 5:   Liquidity            — enough USD in the book to enter/exit?
  Gate 7:   Valid price range    — not at degenerate extremes?
  Gate 4:   Margin-adjusted edge — positive EV after the model-error safety margin?
  Gate 6:   Odds velocity        — fast price movement = informed flow present?
  Gate 8:   Composite confidence — weighted spread + timing + calibration score?
  Gate 9.5: Equal extreme-price   — crowd >85% confident = market has station data.
  Gate 9.6: Equal YES blocked     — model overestimates P(exact hit); 20% WR vs 85% for NO.
  Gate 9.7: Low-priced YES        — market_p < 15¢ YES bets: 9.4% WR, -8.1% ROI (355-trade data).

A Signal is produced for every evaluation. quality_gate_passed=False signals carry
a rejection_reason string naming the gate that blocked them.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .config import (
    BLOCK_EQUAL_YES,
    BOOK_DEPTH_MIN_MULTIPLIER,
    EXTREME_EQUAL_MARKET_THRESHOLD,
    GATE8_CALIB_WEIGHT,
    GATE8_SPREAD_WEIGHT,
    GATE8_TIMING_WEIGHT,
    LEAD_TIME_DECAY_PER_DAY,
    MAX_BOOK_SPREAD,
    MAX_ENTRY_DAYS_AHEAD,
    MAX_ENSEMBLE_SPREAD,
    MAX_FORECAST_AGE_HOURS,
    MAX_MARKET_PRICE,
    MAX_PRICE_VELOCITY_PP,
    MIN_COMPOSITE_CONFIDENCE,
    MIN_ENSEMBLE_MEMBERS,
    MIN_ENTRY_HOURS_AHEAD,
    MIN_MARKET_LIQUIDITY_USD,
    BLOCKED_YES_CITIES,
    MIN_NET_EV_PP,
    MIN_YES_ENTRY_PRICE,
    REQUIRE_STATION_TRUTH,
    EDGE_SAFETY_MARGIN_PP,
    VELOCITY_WINDOW_HOURS,
)
from .city_bias import CityBiasCorrector
from .models import EnsembleForecast, RawProbabilityResult, Signal, WeatherMarket
from .probability_model import ProbabilityModel
from .weather_client import WeatherClient


class SignalGenerator:
    def __init__(
        self,
        model: ProbabilityModel,
        client: WeatherClient,
        price_tracker=None,
        bias_corrector: CityBiasCorrector | None = None,
    ):
        self.model = model
        self.client = client
        self.price_tracker = price_tracker
        self.bias_corrector = bias_corrector or CityBiasCorrector()

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

        now = datetime.now(timezone.utc)
        days_to_res = (market.resolution_date - now).total_seconds() / 86400
        lead_day = max(1, round(days_to_res))
        month = market.resolution_date.month

        # Bias correction. Phase 1 MOS (member-shift inside compute_probability) owns
        # temperature correction where it has data; in that case the flat Phase-2 city
        # bias must stand down to avoid double-correcting. The flat threshold offset is
        # therefore applied ONLY to temperature metrics that MOS does not cover — and
        # never to precipitation (a °C offset on a mm threshold is meaningless).
        corrector = getattr(self.model, "skill_corrector", None)
        mos_covers = corrector is not None and corrector.covers(market.metric)
        is_temp = market.metric in ("temperature_2m_max", "temperature_2m_min")
        if is_temp and not mos_covers:
            # Phase 3: seasonal offset (falls back to all-season within get_offset).
            bias_offset = self.bias_corrector.get_offset(
                market.location.lat, market.location.lon, month
            )
        else:
            bias_offset = 0.0
        adj_threshold      = market.threshold - bias_offset
        adj_threshold_high = (market.threshold_high - bias_offset
                               if market.threshold_high is not None else None)

        prob_result = self.model.compute_probability(
            forecast=forecast,
            threshold=adj_threshold,
            direction=market.direction,
            threshold_high=adj_threshold_high,
            lead_day=lead_day,
            month=month,
            resolve_unit=market.resolve_unit,
        )

        gate_passed, rejection_reason, confidence_score = self._quality_gates(
            market, forecast, prob_result, now, days_to_res
        )

        # Record price AFTER gate check so Gate 6 velocity uses previous prices
        if self.price_tracker is not None:
            self.price_tracker.record(market.market_id, market.yes_price)

        model_p = prob_result.calibrated_p

        # Lead-time + spread shrinkage: shrink model_p toward the MARKET price.
        # skill: 5%/day decay beyond day-1; spread_factor: 0 at MAX_ENSEMBLE_SPREAD.
        # The anchor must be the market price, not 0.5: the no-information limit for
        # a multi-bucket market is the crowd's price, not a coin flip. Shrinking
        # toward 0.5 manufactured phantom YES signals on low-probability buckets
        # (e.g. calibrated_p=0.06 < market 0.22, shrunk to 0.37 > 0.22 → direction
        # flipped to YES purely by shrinkage geometry; YES-range went 0/35 this way).
        # Anchored at market price, shrinkage scales the edge toward zero and can
        # never flip the trade direction.
        skill = max(0.5, 1.0 - LEAD_TIME_DECAY_PER_DAY * max(0.0, days_to_res - 1.0))
        spread_factor = max(0.0, 1.0 - prob_result.ensemble_spread / MAX_ENSEMBLE_SPREAD)
        model_p = market.yes_price + (model_p - market.yes_price) * skill * spread_factor

        # Re-check gate 4 after both shrinkages
        if gate_passed:
            net_ev_shrunk = abs(model_p - market.yes_price) - EDGE_SAFETY_MARGIN_PP
            if net_ev_shrunk < MIN_NET_EV_PP:
                gate_passed = False
                rejection_reason = f"gate4_after_shrinkage:{net_ev_shrunk:.3f}"

        market_p = market.yes_price
        edge_pp = abs(model_p - market_p)
        direction = "YES" if model_p > market_p else "NO"

        # Size factor (0.0–1.0): product of spread and lead-time confidence.
        # Used by live trader for Kelly position sizing; paper trader logs it for analysis.
        size_factor = round(spread_factor * skill, 3)

        return Signal(
            market=market,
            model_p=model_p,
            market_p=market_p,
            edge_pp=round(edge_pp, 4),
            direction=direction,
            ensemble_spread=prob_result.ensemble_spread,
            confidence_score=confidence_score,
            size_factor=size_factor,
            quality_gate_passed=gate_passed,
            rejection_reason=rejection_reason,
            signal_time=now,
            forecast=forecast,
            prob_result=prob_result,
        )

    def _quality_gates(
        self,
        market: WeatherMarket,
        forecast: EnsembleForecast,
        prob: RawProbabilityResult,
        now: datetime,
        days_to_res: float,
    ) -> tuple[bool, str | None, float]:
        """
        Returns (passes, rejection_reason, composite_confidence).
        Composite confidence is 0.0 for any early-rejection gate.
        """

        # Gate 0.5: Verified resolution truth — temperature markets must carry a
        # registered resolving station. Unregistered ones forecast the city grid and
        # resolve on Open-Meteo reanalysis, which disagreed with on-chain settlement
        # on 33% of outcomes (phase-3 backtest, PR #36). No trusted thermometer →
        # no trade; register the station (iem_client._STATION_REGISTRY) to re-enable.
        if (
            REQUIRE_STATION_TRUTH
            and market.metric in ("temperature_2m_max", "temperature_2m_min")
            and not market.station_icao
        ):
            return False, f"gate0.5_no_station_truth:{market.location.city}", 0.0

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

        # Gate 2.6: Model diversity — single-model forecasts have no cross-model
        # spread signal; spread=0.0 would masquerade as maximum agreement.
        if prob.n_models < 2:
            return False, f"gate2.6_single_model:n_models={prob.n_models}", 0.0

        # Gate 2.7: Volatility regime (ensemble spread)
        if prob.ensemble_spread > MAX_ENSEMBLE_SPREAD:
            return False, f"gate2.7_high_spread:{prob.ensemble_spread:.3f}", 0.0

        # Gate 5: Liquidity — checked on the book side the trade actually consumes.
        # The signal direction is implied by calibrated_p vs price (shrinkage anchors
        # at the market price, so it can't flip — same inference gates 9.6-9.8 use).
        # A NO buy consumes NO-token asks; validating YES-ask depth for it checks a
        # book the order never touches (Jul-7 audit).
        # Two separate checks, not a fallback for the same metric:
        #   side ask depth = live CLOB depth (fetched when sidecar is running)
        #     → must be ≥ BOOK_DEPTH_MIN_MULTIPLIER × MIN_MARKET_LIQUIDITY_USD (3×)
        #   liquidity_usd = volumeClob (cumulative, always available)
        #     → used only when depth is unavailable (paper mode / sidecar down)
        side_is_yes = prob.calibrated_p > market.yes_price
        side_depth = market.book_depth_usd if side_is_yes else market.no_book_depth_usd
        side_tag = "yes" if side_is_yes else "no"
        if side_depth > 0.0:
            min_depth = MIN_MARKET_LIQUIDITY_USD * BOOK_DEPTH_MIN_MULTIPLIER
            if side_depth < min_depth:
                return False, f"gate5_low_book_depth_{side_tag}:{side_depth:.0f}_required:{min_depth:.0f}", 0.0
        elif market.liquidity_usd < MIN_MARKET_LIQUIDITY_USD:
            return False, f"gate5_low_liquidity:{market.liquidity_usd:.0f}", 0.0

        # Gate 5.5: Bid-ask spread on the traded side. A wide book silently eats
        # the modeled edge on entry (nothing else caps it — the 12pp edge floor is
        # computed off the Gamma quote, not the executable ask).
        best_ask, best_bid = ((market.yes_best_ask, market.yes_best_bid) if side_is_yes
                              else (market.no_best_ask, market.no_best_bid))
        if best_ask > 0.0 and best_bid > 0.0:
            spread = best_ask - best_bid
            if spread > MAX_BOOK_SPREAD:
                return False, f"gate5.5_wide_spread_{side_tag}:{spread:.3f}_max:{MAX_BOOK_SPREAD}", 0.0

        # Gate 7: Valid price range
        if not (0.0 < market.yes_price < 1.0):
            return False, f"gate7_invalid_price:{market.yes_price}", 0.0
        if market.yes_price < (1 - MAX_MARKET_PRICE) or market.yes_price > MAX_MARKET_PRICE:
            return False, f"gate7_extreme_price:{market.yes_price:.3f}", 0.0

        # Gate 9.5: Extreme equal-market — crowd is >THRESHOLD confident on exact
        # temperature. Evidence shows the market has near-real-time station data
        # in these cases; fading it is systematically losing.
        if market.direction == "equal":
            yes_p = market.yes_price
            if yes_p > EXTREME_EQUAL_MARKET_THRESHOLD or yes_p < (1 - EXTREME_EQUAL_MARKET_THRESHOLD):
                return False, f"gate9.5_extreme_equal_market:{yes_p:.2f}", 0.0

        # Gate 9.6: Equal YES blocked — model overestimates P(exact hit).
        # 160-trade backtest: equal YES = 20% WR; equal NO = 85% WR.
        # Uses pre-shrinkage calibrated_p; since shrinkage anchors at the market price,
        # the post-shrinkage direction always matches, so this is exact.
        if BLOCK_EQUAL_YES and market.direction == "equal" and prob.calibrated_p > market.yes_price:
            return False, "gate9.6_equal_yes_blocked", 0.0

        # Gate 9.7: Low-priced YES blocked — market near-zero on YES, model consistently wrong.
        # 355-trade data: YES bets with market_p < 15¢ → 9.4% WR, -8.1% ROI (53 trades).
        # Cutting these recovers $95 and lifts overall ROI 37% → 45%.
        if prob.calibrated_p > market.yes_price and market.yes_price < MIN_YES_ENTRY_PRICE:
            return False, f"gate9.7_low_priced_yes:{market.yes_price:.3f}", 0.0

        # Gate 9.8: City-specific YES blocks — data-driven per-city YES suppression.
        # 577-trade dataset (Jun 2026): Tokyo YES → 0% WR, -37% ROI (7 trades).
        # Direction inferred from calibrated_p; shrinkage is anchored at market price so
        # direction cannot flip post-shrinkage.
        implied_yes = prob.calibrated_p > market.yes_price
        if implied_yes and any(city in market.title for city in BLOCKED_YES_CITIES):
            matched = next(c for c in BLOCKED_YES_CITIES if c in market.title)
            return False, f"gate9.8_city_yes_blocked:{matched}", 0.0

        # Gate 4: Margin-adjusted edge (blocks the majority of candidate trades)
        gross_ev = abs(prob.calibrated_p - market.yes_price)
        net_ev = gross_ev - EDGE_SAFETY_MARGIN_PP
        if net_ev < MIN_NET_EV_PP:
            return False, f"gate4_margin_adjusted_edge:{net_ev:.3f}_required:{MIN_NET_EV_PP}", 0.0

        # Gate 6: Odds velocity — fast movement signals informed flow
        if self.price_tracker is not None:
            velocity = self.price_tracker.get_velocity(market.market_id, VELOCITY_WINDOW_HOURS)
            if velocity is not None and abs(velocity) > MAX_PRICE_VELOCITY_PP:
                return False, f"gate6_informed_flow:{velocity:+.3f}", 0.0

        # Gate 8: Composite confidence
        # spread_component is 0 when n_models < 2 (single model = no real spread signal)
        if prob.n_models < 2:
            spread_component = 0.0
        else:
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
