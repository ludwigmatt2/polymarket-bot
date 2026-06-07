"""
Central configuration for the weather arbitrage bot.
All thresholds and constants live here — no magic numbers in other modules.
"""

# ── Signal quality thresholds ──────────────────────────────────────────────────
MIN_NET_EV_PP = 0.04            # Gate 4: minimum edge after subtracting round-trip fees
MAX_DAYS_TO_RESOLUTION = 31     # Include monthly (May) markets
MAX_ENSEMBLE_SPREAD = 0.20      # Allow slightly more uncertainty for monthly markets
MIN_ENSEMBLE_MEMBERS = 3        # Minimum model count for a valid ensemble

# ── Entry timing (Gate 1) ──────────────────────────────────────────────────────
MAX_ENTRY_DAYS_AHEAD = 5        # Reject if resolution is more than 5 days out
MIN_ENTRY_HOURS_AHEAD = 4       # Reject if resolution is less than 4 hours away

# ── Forecast freshness (Gate 0) ───────────────────────────────────────────────
MAX_FORECAST_AGE_HOURS = 6      # Reject if Open-Meteo data is older than 6 hours

# ── Odds velocity / informed flow (Gate 6) ────────────────────────────────────
MAX_PRICE_VELOCITY_PP = 0.15    # Block if price moved >15pp within the velocity window
VELOCITY_WINDOW_HOURS = 6       # Rolling window for velocity measurement

# ── Composite confidence (Gate 8) ─────────────────────────────────────────────
MIN_COMPOSITE_CONFIDENCE = 0.30  # Weighted score of spread + timing + calibration
GATE8_SPREAD_WEIGHT = 0.40
GATE8_TIMING_WEIGHT = 0.35
GATE8_CALIB_WEIGHT = 0.25

# ── Market filters ─────────────────────────────────────────────────────────────
MIN_MARKET_LIQUIDITY_USD = 50.0  # Lowered to include monthly precipitation markets
BOOK_DEPTH_MIN_MULTIPLIER = 3   # Gate 5: require N× min liquidity in live CLOB book depth
MIN_MARKET_PRICE = 0.03         # Avoid illiquid extremes
MAX_MARKET_PRICE = 0.97
# Gate 9.5: skip "equal" direction markets where crowd is this confident —
# evidence shows they are pricing on near-real-time station data we don't have
EXTREME_EQUAL_MARKET_THRESHOLD = 0.85

# ── Equal-market direction filter ─────────────────────────────────────────────
# Data (160 resolved trades): equal NO bets → 85% WR; equal YES bets → 20% WR.
# Root cause: predicting "temperature EXACTLY = X°" (YES) requires sub-0.5°C
# ensemble precision the model doesn't achieve — systematic overestimation of P(hit).
# Predicting "temperature WON'T be exactly X" (NO) is structurally easier and
# consistently profitable. Block equal YES bets until model can demonstrate
# calibrated P(exact hit) that beats market pricing.
BLOCK_EQUAL_YES = True

# ── Low-priced YES gate ────────────────────────────────────────────────────────
# Data (355 resolved trades): YES bets where market_p < 15¢ → 9.4% WR, -8.1% ROI.
# The market is near-zero on YES; our model says otherwise but is consistently wrong.
# Cutting these 53 trades recovers $95 in losses and lifts overall ROI 37% → 45%.
MIN_YES_ENTRY_PRICE = 0.15

# ── Fee model ──────────────────────────────────────────────────────────────────
TAKER_FEE_PER_SIDE = 0.02       # 2% per trade (Polymarket CLOB taker)
ROUND_TRIP_FEE = 0.04           # 4% total

# ── Paper trading ──────────────────────────────────────────────────────────────
PAPER_TRADE_SIZE_USD = 25.0

# ── Live trading (Kelly sizing) ────────────────────────────────────────────────
KELLY_FRACTION = 0.25           # Quarter Kelly — conservative for uncertain edge
MAX_LIVE_TRADE_USD = 25.0       # Hard cap per trade during validation period
DAILY_LOSS_LIMIT_PCT = 0.05     # Kill switch at -5% of total capital

# ── Go-live gates (all must pass before real money) ────────────────────────────
MIN_RESOLVED_TRADES = 20
MIN_PROFIT_FACTOR = 1.5         # Gross wins / gross losses
MIN_BRIER_SKILL_SCORE = 0.0     # Must beat climatology (BSS ≥ 0)
MAX_PAPER_DRAWDOWN_PCT = 0.20   # Max hypothetical drawdown allowed

# ── Open-Meteo API ────────────────────────────────────────────────────────────
OPEN_METEO_ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
OPEN_METEO_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
# Previous Runs API: lead-time-specific past forecasts (temperature_2m_previous_dayN).
# Source for Phase 1 historical-skill / MOS error stats. Retained back to ~Jan 2024.
OPEN_METEO_PREVIOUS_RUNS_URL = "https://previous-runs-api.open-meteo.com/v1/forecast"
OPEN_METEO_REQUEST_TIMEOUT = 15  # seconds

# ── Phase 1 — Historical skill / MOS correction ────────────────────────────────
# Minimum forecast-error observations in a (city, lead, month) cell before the MOS
# member-shift is trusted and applied; thin cells fall back (month→0, then nearest lead).
MIN_SKILL_OBS = 30
HISTORICAL_SKILL_PATH = "logs/historical_skill.json"
# Metrics MOS corrects (validated out-of-sample to beat raw AND flat city bias by
# +6–8% / +4–7% MAE on the 2024+ Previous-Runs record). MOS owns temperature
# correction; where MOS covers a metric, the flat Phase-2 city bias is NOT also
# applied (it would double-correct). Precipitation is excluded (non-Gaussian).
MOS_METRICS = frozenset({"temperature_2m_max", "temperature_2m_min"})
# Kill switch. MOS is validated on out-of-sample forecast MAE (the operational MOS
# standard), which is a proxy for live Brier; the definitive per-direction Brier
# check accrues over ~2–3 weeks via the exact-replay harness. Flip to False to
# disable the member-shift instantly if that native check ever regresses.
MOS_ENABLED = True

# ── Lead-time skill decay ──────────────────────────────────────────────────────
LEAD_TIME_DECAY_PER_DAY = 0.05   # Shrink model_p 5% per day beyond day-1 toward 0.5

# Ensemble model IDs recognized by Open-Meteo /v1/ensemble
# GFS: 31 members; ICON-EPS: 40 members; ECMWF IFS: 50 members
ENSEMBLE_MODELS = ["gfs_seamless", "icon_seamless", "ecmwf_ifs025"]

# ── Phase 4 — Per-model skill weighting ────────────────────────────────────────
# Member-level weights: each model's ensemble members are weighted by these factors
# when computing the weighted fraction + weighted KDE, amplifying the more skillful
# model beyond its raw member count (ECMWF already has the most members). This is a
# LABELED LITERATURE PRIOR (ECMWF is the most skillful global model at 3–7 day leads),
# empirically confirmed for our cities via Previous-Runs lead-3 MAE (ECMWF<ICON<GFS;
# inverse-MAE ≈ 1.0/1.2/1.4). model_skill_tracker.py replaces it with fitted weights
# once enough resolved trades carry model_breakdown_json. Equal weights reproduce the
# pre-Phase-4 member-pooled behavior exactly.
MODEL_WEIGHTS = {"ecmwf_ifs025": 1.5, "icon_seamless": 1.1, "gfs_seamless": 1.0}
# Kill switch — False reverts to equal member pooling (no weighting).
MODEL_WEIGHTING_ENABLED = True

# Deterministic models used for cross-model spread (uncertainty proxy)
FORECAST_MODELS = ["gfs_seamless", "ecmwf_ifs025", "icon_seamless"]

# ── Weather market keywords (used to search Polymarket for weather markets) ────
WEATHER_SEARCH_TERMS = [
    # Generic weather terms
    "highest temperature",
    "lowest temperature",
    "precipitation",
    # City-specific — ensures daily markets for all known cities are captured
    "temperature London",
    "temperature New York",
    "temperature NYC",
    "temperature Paris",
    "temperature Hong Kong",
    "temperature Tokyo",
    "temperature Madrid",
    "temperature Toronto",
    "temperature Seoul",
    "temperature Miami",
    "temperature Atlanta",
    "temperature Dallas",
    "temperature Tel Aviv",
    "temperature Berlin",
    "temperature Sydney",
    "temperature Dubai",
]
# Terms deliberately excluded (return sports teams or unrelated results):
# "temperature" (generic) → mostly returns duplicates of city-specific results
# "celsius" / "fahrenheit" → rarely returns weather markets
# "hurricane"  → Carolina Hurricanes (NHL)
# "heat"       → Miami Heat (NBA)
# "snow"       → Edward Snowden
# "weather"    → Space Weather events
# "tornado", "wind speed", "storm surge", "rainfall" → too few/irrelevant results
