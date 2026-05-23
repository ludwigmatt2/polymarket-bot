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
MIN_MARKET_PRICE = 0.03         # Avoid illiquid extremes
MAX_MARKET_PRICE = 0.97
# Gate 9.5: skip "equal" direction markets where crowd is this confident —
# evidence shows they are pricing on near-real-time station data we don't have
EXTREME_EQUAL_MARKET_THRESHOLD = 0.85

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
OPEN_METEO_REQUEST_TIMEOUT = 15  # seconds

# ── Lead-time skill decay ──────────────────────────────────────────────────────
LEAD_TIME_DECAY_PER_DAY = 0.05   # Shrink model_p 5% per day beyond day-1 toward 0.5

# Ensemble model IDs recognized by Open-Meteo /v1/ensemble
# GFS: 31 members; ICON-EPS: 40 members; ECMWF IFS: 50 members
ENSEMBLE_MODELS = ["gfs_seamless", "icon_seamless", "ecmwf_ifs025"]

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
