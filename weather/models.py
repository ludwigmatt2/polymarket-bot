"""
Shared dataclasses for the weather bot.
Kept in one file to avoid circular imports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any


@dataclass
class Location:
    city: str
    lat: float
    lon: float
    timezone: str = "auto"
    country: str = ""


@dataclass
class WeatherMarket:
    market_id: str
    title: str
    yes_price: float        # Current market-implied P(YES)
    liquidity_usd: float
    resolution_date: datetime
    resolution_source: str  # e.g. "NOAA", "Weather Underground"
    location: Location
    metric: str             # "temperature_2m_max", "precipitation_sum", etc.
    threshold: float        # lower bound (or only bound for above/below markets)
    direction: str          # "above", "below", "equal", or "range"
    url: str
    threshold_high: float | None = None  # upper bound for range markets ("between X and Y")
    raw_title: str = ""     # Original title before parsing
    # For monthly aggregate markets (>7d horizon), forecast is summed over a date window
    forecast_start_date: "date | None" = None  # set for monthly markets
    # Current CLOB order-book depth; 0.0 = not fetched
    book_depth_usd: float = 0.0
    # CLOB outcome token IDs — captured from Gamma API clobTokenIds field
    yes_token_id: str = ""
    no_token_id: str = ""
    # Minimum tick size as string literal required by PartialCreateOrderOptions
    tick_size: str = "0.01"


@dataclass
class EnsembleForecast:
    lat: float
    lon: float
    target_date: date
    metric: str
    # Per-model member arrays: {"gfs_seamless": [88.1, 89.4, ...], ...}
    member_arrays: dict[str, list[float]] = field(default_factory=dict)
    # Deterministic per-model values for spread calculation
    model_means: dict[str, float] = field(default_factory=dict)
    fetched_at: datetime = field(default_factory=datetime.utcnow)
    # Historical full_month / first_7_days ratio applied to project monthly aggregates;
    # None for single-date forecasts.
    scaling_ratio: float | None = None

    @property
    def all_members(self) -> list[float]:
        """Flat list of all ensemble member values across all models."""
        result = []
        for vals in self.member_arrays.values():
            result.extend(vals)
        return result

    @property
    def n_members(self) -> int:
        return len(self.all_members)

    @property
    def ensemble_mean(self) -> float:
        members = self.all_members
        return sum(members) / len(members) if members else 0.0

    @property
    def ensemble_std(self) -> float:
        """Spread across model means — proxy for forecast uncertainty."""
        if len(self.model_means) < 2:
            return 0.0
        vals = list(self.model_means.values())
        mean = sum(vals) / len(vals)
        variance = sum((v - mean) ** 2 for v in vals) / len(vals)
        return variance ** 0.5


@dataclass
class RawProbabilityResult:
    raw_p: float                        # Fraction of ensemble members satisfying condition
    calibrated_p: float                 # After isotonic correction (== raw_p if uncalibrated)
    ensemble_spread: float              # Std of model means (uncertainty proxy)
    n_members: int
    is_calibrated: bool
    model_breakdown: dict[str, float]   # Per-model sub-probabilities
    threshold: float
    direction: str
    metric: str
    n_models: int = 0                   # Distinct models that contributed to model_breakdown


@dataclass
class Signal:
    market: WeatherMarket
    model_p: float          # Calibrated model probability (after all shrinkages)
    market_p: float         # Current market price
    edge_pp: float          # abs(model_p - market_p) — always positive
    direction: str          # "YES" or "NO" (which contract to buy)
    ensemble_spread: float
    confidence_score: float # Gate 8 composite score
    size_factor: float      # 0.0–1.0: spread × lead-time confidence (for position sizing)
    quality_gate_passed: bool
    rejection_reason: str | None
    signal_time: datetime
    forecast: EnsembleForecast
    prob_result: RawProbabilityResult

    @property
    def entry_price(self) -> float:
        """Market-implied cost per contract for the signalled direction."""
        return self.market_p if self.direction == "YES" else (1.0 - self.market_p)


@dataclass
class PaperTrade:
    trade_id: str
    market_id: str
    market_title: str
    signal_time: datetime
    entry_price: float      # Market price at signal time
    model_p: float
    direction: str          # "YES" or "NO"
    size_usd: float
    size_factor: float      # 0.0–1.0 spread×lead-time weight applied to size_usd
    edge_pp: float
    ensemble_spread: float
    confidence_score: float
    resolution_date: datetime
    # Stored at log time so auto-resolve never needs to re-parse the title
    metric: str = ""
    threshold: float = 0.0
    threshold_high: float | None = None
    weather_direction: str = ""  # "above"/"below"/"equal"/"range"
    lat: float = 0.0
    lon: float = 0.0
    location_tz: str = "UTC"     # timezone used when this trade was forecast; E1 fix
    # Phase 0: persist the calibrator input (raw_p) and per-model breakdown so the
    # calibrator trains on the same scale it is applied to, and so per-model skill
    # (Phase 4) can be scored from history. raw_p is pre-calibration, pre-shrinkage.
    raw_p: float = 0.5
    model_breakdown_json: str = ""   # json.dumps(prob_result.model_breakdown)
    actual_outcome: bool | None = None
    resolved_at: datetime | None = None
    pnl_usd: float | None = None
    brier_score: float | None = None


@dataclass
class PaperTradingStats:
    total_trades: int
    resolved_trades: int
    win_rate: float
    profit_factor: float        # sum(wins) / sum(losses) in USD
    mean_brier_score: float
    brier_skill_score: float    # 1 - (mean_brier / 0.25); 0 = climatology
    total_paper_pnl: float
    avg_edge_pp: float
    max_drawdown_pct: float
    ready_for_live: bool
    failure_reasons: list[str]  # Why not ready, if applicable
