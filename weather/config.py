"""
Central configuration for the weather arbitrage bot.
All thresholds and constants live here — no magic numbers in other modules.
"""

import os

# ── Signal quality thresholds ──────────────────────────────────────────────────
MIN_NET_EV_PP = 0.08            # Gate 4: minimum edge after subtracting round-trip fees
                                # Raised 0.04→0.08 (Jun 2026): 5-10% gross-edge trades earned
                                # only +28.6% ROI vs +50.8% for >20% edge (577-trade dataset).
# Gate 4.5: edge CEILING — reject signals claiming MORE gross edge than this.
# The model is most wrong exactly when it claims the most disagreement with the
# market: >0.20pp edge ran PF 0.66-0.81 in BOTH temporal halves of the 1,023-trade
# station-truth backtest (Jul 2026) AND lost money forward (n=42, Jul 8-Aug 4).
# A huge claimed edge is evidence of model error, not opportunity.
MAX_EDGE_PP = 0.20
MAX_DAYS_TO_RESOLUTION = 31     # Include monthly (May) markets
MAX_ENSEMBLE_SPREAD = 0.20      # Allow slightly more uncertainty for monthly markets
MIN_ENSEMBLE_MEMBERS = 3        # Minimum model count for a valid ensemble

# ── Entry timing (Gate 1) ──────────────────────────────────────────────────────
MAX_ENTRY_DAYS_AHEAD = 7        # Reject if resolution is more than 7 days out
                                # Raised 5→7 (Jun 2026): d=3-4 trades show +69%/+43% ROI vs
                                # +36-39% at d=0-1; also rescales Gate 8 timing_component so
                                # 3-4 day markets receive higher confidence and are not
                                # unfairly penalised by the timing term.
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

# ── Scanner health alarm ───────────────────────────────────────────────────────
# Healthy scans parse ~90% of fetched markets; the Jun 2026 E4 regression ran at
# ~10% for 7 days with zero alerts. Alarm (log + scanner_alarm.csv → Telegram)
# when the rate drops below this. Only checked when enough markets were fetched
# for the rate to be meaningful.
MIN_PARSE_RATE = 0.30
MIN_FETCHED_FOR_PARSE_ALARM = 50

# ── Market filters ─────────────────────────────────────────────────────────────
# Gate 0.5: only trade temperature markets whose resolving station is in the IEM
# registry, so forecast target and settlement read the same thermometer. Grid-truth
# labels disagreed with on-chain resolution on 33% of outcomes (phase-3 backtest);
# an unregistered city is untradeable until its station is added and verified.
REQUIRE_STATION_TRUTH = True

MIN_MARKET_LIQUIDITY_USD = 50.0  # Lowered to include monthly precipitation markets
BOOK_DEPTH_MIN_MULTIPLIER = 3   # Gate 5: require N× min liquidity in live CLOB book depth
# Gate 5.5: max bid-ask spread on the traded side. A 4¢+ book eats a third of the
# 12pp edge floor on entry; nothing else caps this (edge is computed off the Gamma
# quote). Only enforced when both best quotes were fetched (live sidecar up).
MAX_BOOK_SPREAD = 0.04

# Running-extreme clip: on a market's event day, fetch the station's live
# running max/min (IEM METAR, ~5-15 min latency) and clip ensemble members at
# it — the final daily max is max(observed-so-far, rest-of-day), so this is
# exact math. It both creates signals (market slow to react to the tape) and
# protects (never fade a market watching a live feed we didn't have — the
# adverse-selection channel Gate 9.5 crudely approximates).
#
# DISABLED Aug 2026: 26 days of forward validation split by this feature —
# clip fired: PF 0.67 (−$455, n=113); clip off: PF 1.22 (+$195, n=105). The
# clip asserts near-certainty while the day still has room to move (obs lag,
# whole-degree rounding at the boundary, temp still rising past the peak-so-far
# reading): clipped trades averaged model_p 0.43 vs a 0.59 real outcome rate.
# Re-enable only WITH a proper fix (widened uncertainty near the clip bound).
# Side effect (intended): observed_c stays None → the Gate-1 late-window
# bypass (intraday_ok) closes, so the intraday loop stops logging late-window
# trades — that was the losing segment. Loop + watchlist plumbing stays.
RUNNING_OBS_ENABLED = False

# ── Intraday event-day loop (I1) ───────────────────────────────────────────────
# Between hourly full scans, re-evaluate ONLY the event-day station markets
# (handed over via the watchlist file) with fresh observations and fresh
# executable book quotes. This is where the running-max clip earns: the tape
# decides buckets intraday and an hourly sample misses most of the reprice
# window. A stale watchlist (no full scan for 2h) stands the loop down.
INTRADAY_SCAN_INTERVAL_S = 900
WATCHLIST_MAX_AGE_S = 7200

# X1 exit simulation: sell (on paper) when the book BIDS more than the model
# says the position is worth, by at least this margin — i.e. the market
# overvalues our position under our own beliefs (the canonical trigger: the
# tape killed our bucket and someone still bids 40-60¢ for it). The margin
# respects model error and avoids churn; tune from the exit-vs-hold comparison
# once the counterfactual stream accumulates.
EXIT_MARGIN_PP = 0.05
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

# ── City-specific YES blocks (Gate 9.8) ───────────────────────────────────────
# Data (577 resolved trades, Jun 2026): Tokyo YES bets → 0% WR, -37% ROI (7 trades).
# Tokyo NO remains tradeable (+19.8% ROI). Other cities may be added as data accumulates.
BLOCKED_YES_CITIES: list[str] = ["Tokyo"]

# ── Edge safety margin ─────────────────────────────────────────────────────────
# Polymarket charges NO fees on these markets, positions are held to resolution,
# and redemption is free — the old "ROUND_TRIP_FEE=0.04" was fiction. What the
# haircut actually pays for is MODEL ERROR / adverse selection: when the model
# disagrees with the market, part of that gap is the model being wrong, and the
# marginal trades are where miscalibration lives (Jul-7 audit calibration curve).
# Reduced 4pp → 3pp at the Jul-8 paper reset; to be tuned empirically from the
# realized-vs-modeled edge curve once ~150+ station-resolved trades exist.
# Execution costs are handled explicitly elsewhere (Gate 5.5 spread cap + the
# edge-preserving FAK price cap), NOT by this margin.
EDGE_SAFETY_MARGIN_PP = 0.03

# ── Paper trading ──────────────────────────────────────────────────────────────
PAPER_TRADE_SIZE_USD = 25.0
# Virtual bankroll the paper record is measured against (display/ROI only — flat
# $25 sizing = 2.5% of it per trade). Set at the Jul-8 paper reset: live paused,
# fresh $1,000 forward-validation run on the fixed model (rounding pre-image +
# station truth), old history archived to paper_trades.pre_reset_*.bak.
PAPER_BANKROLL_USD = 1000.0

# ── Live trading (Kelly sizing) ────────────────────────────────────────────────
# Eighth Kelly (0.25 → 0.125, Aug 2026): Kelly sizing off a miscalibrated
# probability compounds the damage — the forward calibration curve showed raw_p
# in [0,0.10) resolving YES 34.9% of the time. Restore quarter Kelly only after
# the variance-inflation calibration fix is validated forward.
KELLY_FRACTION = 0.125
MAX_LIVE_TRADE_USD = float(os.environ.get("MAX_LIVE_TRADE_USD", "25.0"))  # overrideable via env or /setmaxbet
# Kill switch: halt for the day when resolved losses exceed
# max(DAILY_LOSS_LIMIT_PCT × bankroll, 2 × max order) — the floor keeps a small
# bankroll from tripping on a single normal-sized losing trade.
DAILY_LOSS_LIMIT_PCT = 0.05
MAX_SLIPPAGE = float(os.environ.get("MAX_SLIPPAGE", "0.03"))  # market-buy price cap above entry (thin books)
# Exposure caps: one bin per event (adjacent bins of the same temperature ladder
# share the same forecast error — the bot would otherwise concentrate NO bets
# exactly where its miss lands), and a cap on total USD resolving the same day
# (the whole book marks overnight in one batch while the kill switch is blind).
MAX_DAY_EXPOSURE_PCT = 0.40

# ── Go-live gates (all must pass before real money) ────────────────────────────
MIN_RESOLVED_TRADES = 20        # legacy floor (overall record; display only)
MIN_PROFIT_FACTOR = 1.5         # Gross wins / gross losses
MIN_BRIER_SKILL_SCORE = 0.0     # Must beat climatology (BSS ≥ 0) — display only
# Calibrator sanity guard: a learned correction may move raw_p by at most this
# much. Calibration exists to fix systematic bias, not to overrule the model —
# the old poisoned calibrator compressed every raw_p into ~0.35-0.49 and once
# INVERTED a 99.9% raw certainty into a NO bet (Jul-10 Shanghai total loss).
# With the cap, extreme confidence can be tempered but never flipped across the
# coin-flip line (0.999 → ≥0.749). Structural guard, active for every future
# calibrator fit — not a patch for one bad dataset.
MAX_CALIBRATION_SHIFT = 0.25

# ── Re-live gate (Jul-8 redesign) ──────────────────────────────────────────────
# Going live again requires evidence from the FIXED system only: trades resolved
# on verified truth (label_source == "station" or "onchain"), enough of them,
# spanning enough calendar time that one weather regime can't flatter the
# record, profitable, and — the honest skill test — the model's Brier must beat
# the MARKET PRICE's Brier on the same trades (beating climatology is trivial;
# edge means beating the crowd). The gate unlocks live order placement;
# flipping mode stays manual.
#
# LOWERED Aug 20 2026 — explicit user decision, not a re-derived design value.
# Original design: 150 / 21. Actual record at the time: 91 verified trades /
# 14 days, PF 2.04 (>>1.5), model Brier 0.218 < market 0.241, drawdown 7.7%
# (<<20% cap) — every QUALITY check already cleared comfortably; only sample
# size and calendar span were short. Lowered to match what's actually
# accumulated (not to zero) so a real go-live test could run on ~$40 of test
# capital in the reconnected July wallet, ahead of full statistical
# confidence. RESTORE TO 150 / 21 before trusting this gate again for anything
# beyond small deliberate tests — these are not the calibrated thresholds.
GATE_MIN_STATION_RESOLVED = 90
GATE_MIN_DAYS_ELAPSED = 14
# Era boundary: only trades SIGNALED after this instant count for the gate.
# A validation record must measure ONE system (the Jul-9 lesson). Set to the
# actual Phase-1+2 deploy instant (service restarted 2026-08-06 ~12:27 UTC on
# the new code): clip disabled, edge ceiling added, variance inflation λ=2.0.
# The prior era's 206 trades (PF 0.90, clip-tainted, λ=1.0) belong to the old
# system's record; trades signaled Aug-5..deploy were also still old-code, so
# the boundary is the restart, not a round date. Fresh 0/150 from here; the
# topline history keeps every trade.
GATE_ERA_START = "2026-08-06T12:27"
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
MODEL_WEIGHTS = {"ecmwf_ifs025": 1.1, "icon_seamless": 1.1, "gfs_seamless": 1.0}
# Kill switch — False reverts to equal member pooling (no weighting).
MODEL_WEIGHTING_ENABLED = True

# ── Variance inflation (EMOS-lite, Aug 2026) ───────────────────────────────────
# Raw ensembles are UNDERDISPERSED: the forward calibration curve showed raw_p
# in [0,0.10) resolving YES 34.9% of the time — the members' spread understates
# real forecast uncertainty, so tail buckets get near-zero probability that
# reality contradicts. Standard post-processing fix (EMOS-lite): widen each
# model's members about their own mean, v' = mean + λ·(v − mean), BEFORE the
# clip/pre-image/counting stages, so raw counting and KDE both see the wider
# distribution. λ is fit OFFLINE by Brier-sweep on the ECMWF+GEFS replay
# harness (scripts/historical_backtest.py --lambda-sweep); 1.0 = no-op.
# The MAX_CALIBRATION_SHIFT clamp stays untouched as the downstream safety net.
#
# λ = 2.0 fit Aug 2026: three sweeps on a growing replay window (Jul 4-13 →
# 711 buckets, Jul 4-17 → 1029, Jul 4-20 → 1218) all bottomed at λ 2.0-2.2
# with BOTH temporal halves agreeing; the Brier curve is flat across 2.0-2.4.
# The raw ensemble's spread needs ≈doubling to match reality. This closes the
# tail miscalibration but NOT the full gap to the market (model Brier ~0.144
# vs market ~0.119 on the same buckets) — the calibrator retrain stacks on top.
VARIANCE_INFLATION = 2.0
VARIANCE_INFLATION_ENABLED = True

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

# ── Withdrawal hardening (SECURITY_PLAN Phase C) ───────────────────────────────
# The only irreversible money-loss path. Withdrawals may only go to an address the
# user has explicitly allowlisted, and only after a cooling-off window (defeats a
# smash-and-grab if the bot/session is briefly compromised). Daily total is capped;
# large single withdrawals need a re-entered confirmation code; attempts are rate
# limited. All overrideable via env.
WITHDRAW_COOLING_OFF_HOURS   = float(os.environ.get("WITHDRAW_COOLING_OFF_HOURS", "24"))
WITHDRAW_DAILY_CAP_USD       = float(os.environ.get("WITHDRAW_DAILY_CAP_USD", "500"))
WITHDRAW_LARGE_USD           = float(os.environ.get("WITHDRAW_LARGE_USD", "250"))
WITHDRAW_MAX_ATTEMPTS_PER_HR = int(os.environ.get("WITHDRAW_MAX_ATTEMPTS_PER_HR", "5"))
