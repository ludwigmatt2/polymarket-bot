# Weather Model Upgrade Plan

**Goal:** Maximize prediction accuracy within free resources (Open-Meteo, no paid APIs).
**Context for Opus 4.8:** This is a weather arbitrage bot on Polymarket. It predicts daily
temperature outcomes (high/low temp for a city, with YES/NO bets). The model currently
achieves 64.8% WR and 37% ROI across 355 paper trades. The main weakness is YES bets
(structural overestimation of P(exact temperature hit)), while NO bets are highly profitable
(85% WR on exact-range questions).

> **READ THIS FIRST — why this plan was revised.** An earlier version of this plan led with a
> "backfill the calibration log in 30 min for +5–10% Brier" quick win. That step is unsafe as
> originally written and would make the model *worse*. The reason is a data-flow bug that the
> first draft missed (details in Phase 0). The corrected ordering below fixes the bug first,
> adds a way to *measure* every change, and only then touches the model. **Do the phases in the
> order written.** Do not skip Phase 0 or Phase 0.5 — every later phase depends on them.

---

## Current Stack (as-is)

```
WeatherClient                 → Open-Meteo /v1/ensemble
  - GFS seamless  (31 members)
  - ICON-EPS      (40 members)
  - ECMWF IFS025  (50 members)   ← already in ENSEMBLE_MODELS
  Total: ~121 members per forecast

ProbabilityModel  (weather/probability_model.py)
  pipeline:  raw_p  → KDE smoothing → calibrated_p
  - raw_p:        _fraction_satisfying() — members satisfying threshold / total
  - KDE:          _apply_kde() — Gaussian KDE, Scott bandwidth, applied when ≥10 members
  - calibrated_p: _apply_calibration(raw_p, direction)
                    · Platt (logistic) below PLATT_THRESHOLD=300 obs
                    · Isotonic above 300 obs
                    · per-direction calibrators (equal/range/above/below) when enough data
  - calibration source: logs/calibration_log.csv  (currently 35 rows)

SignalGenerator  (weather/signal_generator.py)
  model_p = prob_result.calibrated_p
  model_p = 0.5 + (model_p - 0.5) * skill * spread_factor   ← lead-time + spread SHRINKAGE
  → Signal.model_p is calibrated AND shrunk. This is what paper_trades.csv stores.

CityBiasCorrector  (weather/city_bias.py)
  - Flat offset per city (lat/lon nearest-neighbor, <100 km)
  - Loads ONLY rows with reliable=1: Seoul, Hong Kong, NYC, London
  - Atlanta (+2.62), Paris (−1.88), Tel Aviv (−1.58), Madrid, Toronto, Dallas
    have reliable=0 and are silently dropped at load time.
```

**Root cause of YES underperformance:** For `equal` direction bets (exact temperature = X°C),
the model estimates the probability density inside a ±0.5°C window. With 121 members spread
over ~4–5°C, raw fraction counting is coarse, and KDE with Scott's bandwidth (~0.9°C) smears
density into the window. The *systematic* part of this error is exactly what a per-direction
`equal` calibrator is supposed to remove — but the calibrator is currently fed the wrong
training signal (see Phase 0), so it can't. Fix the calibrator first; only then decide whether
a manual bandwidth tweak is still needed.

---

## Phase 0 — Stop the Calibration Leak (DO THIS FIRST — ~1 session)

This is the most important change in the document. Nothing downstream is trustworthy until it
lands.

### 0.1 The bug

The calibrator must map `raw_p → P(actual)` because at inference it is applied to `raw_p`:

```python
# probability_model.py:75
calibrated_p = self._apply_calibration(raw_p, direction)   # ← input is raw_p
```

But the value logged for calibration is the **calibrated + shrunk** `model_p`, not `raw_p`:

```python
# signal_generator.py
model_p = prob_result.calibrated_p                              # already calibrated
model_p = 0.5 + (model_p - 0.5) * skill * spread_factor        # then shrunk toward 0.5
# → stored as Signal.model_p → paper_trades.csv "model_p"

# paper_trader.py:232  (on resolution)
model.log_observation(model_p, outcome, direction=w_dir)       # ← logs the WRONG quantity
```

So `logs/calibration_log.csv` is training the calibrator on values that have *already* been
calibrated and shrunk, then the result is applied to `raw_p` — a different scale. This is a
train/inference mismatch. It exists **today** in the 35-row log; the original "backfill 355
rows" plan would have scaled it 10× while reporting "calibration active".

**Consequence for backfill:** the historical `raw_p` was never persisted anywhere
(`paper_trades.csv` has no `raw_p` column — verified). It cannot be reconstructed: it depended
on the exact ensemble member spread at signal time, which is gone and not reproducible from the
archive. **Therefore the 355 historical trades cannot seed the calibrator.** Honest accounting:
we restart calibration from clean data going forward. There is no instant Isotonic activation.

### 0.2 Fix — persist `raw_p` (and per-model breakdown) and log the right value

1. **`weather/models.py`** — add fields to `PaperTrade`:
   ```python
   raw_p: float = 0.5                 # pre-calibration, pre-shrinkage fraction (calibrator input)
   model_breakdown_json: str = ""     # json.dumps(prob_result.model_breakdown) for Phase 3
   ```

2. **`weather/paper_trader.py`**
   - Add `"raw_p"` and `"model_breakdown_json"` to `CSV_HEADERS` (append at the end so existing
     rows still parse — `DictReader` tolerates missing trailing columns).
   - In `log_trade()`, populate them from the signal:
     ```python
     raw_p=signal.prob_result.raw_p,
     model_breakdown_json=json.dumps(signal.prob_result.model_breakdown),
     ```
   - In the resolve loop (~line 232), log **raw_p**, not model_p:
     ```python
     raw_p = float(t.get("raw_p") or t["model_p"])   # fall back for pre-Phase-0 rows
     if model is not None:
         model.log_observation(raw_p, outcome, direction=w_dir)
     ```
     Keep `model_p` for PnL/Brier scoring (those are correct — they measure the *final*
     forecast). Only the calibrator input changes.

3. **`Signal.model_p`** already carries `prob_result`, so no signal_generator change is needed
   beyond confirming `Signal` is constructed with `prob_result=prob_result` (it is, line ~153).

### 0.3 Reset the poisoned calibration log

The existing 35 rows are mislabeled (calibrated values posing as raw). Move them aside so the
calibrator starts clean:

```bash
mv logs/calibration_log.csv logs/calibration_log.poisoned.bak
```

The model runs **uncalibrated** (`_apply_calibration` returns `raw_p` unchanged) until
MIN_CALIBRATION_OBS=30 clean rows accumulate. That is correct behavior — better uncalibrated
than mis-calibrated.

**Impact:** Calibration becomes *correct* but no longer instant. Expect ~30 clean obs (≈ a few
weeks at current trade rate) before the global calibrator re-activates, and longer for
per-direction `equal`. This is the price of fixing the bug honestly. Phase 1 (MOS) provides an
archive-sourced calibration path that does **not** wait on live trades — which is why it is now
promoted ahead of the bandwidth hack.

---

## Phase 0.5 — Backtest Harness (DO THIS SECOND — ~1 session)

Every later phase quotes a "+X% Brier/accuracy". On a 355-sample, 64.8%-baseline dataset, you
cannot tell a real improvement from noise without a frozen replay. Build the measuring stick
before changing what it measures.

### 0.5.1 `scripts/backtest.py`

- Load all resolved rows from `logs/paper_trades.csv` (355 with non-empty `actual_outcome`).
- For each, recompute the model output under a **candidate config** and compare to `actual_outcome`.
  - For changes that only touch post-`raw_p` stages (calibration, shrinkage, city bias, model
    weighting): replay is exact, because `raw_p` and `model_breakdown_json` are persisted from
    Phase 0 onward. **Pre-Phase-0 rows lack `raw_p`** — exclude them or mark them `replay=partial`.
  - For changes that touch `raw_p` itself (KDE bandwidth, Phase 1 MOS member-shift): you need the
    raw ensemble members, which aren't stored. Use the Open-Meteo **archive** ensemble endpoint
    to refetch members for `(lat, lon, resolution_date, metric)` and recompute. Cache refetched
    members to `logs/backtest_cache/` so reruns are fast.
- Report, split by direction (equal/range/above/below) and by YES/NO:
  `Brier`, `accuracy`, `mean P`, `coverage`, and `Δ vs baseline config`.
- Add a `--config` flag so a phase can run `baseline` vs `candidate` and print the delta.

**Acceptance rule for every later phase:** a change ships only if `backtest.py` shows a
non-trivial Brier improvement on the affected direction split, not just in aggregate.

---

## Phase 1 — Historical MOS / Forecast-Error Correction (PROMOTED — biggest win, 4–6 hr)

Moved to the front of the model work because it is the **only** calibration source that does
not depend on the (now-restarted) live calibration log, and it directly attacks the exact-hit
overestimation that the old plan tried to patch with bandwidth tuning.

**Concept:** Non-parametric MOS (Model Output Statistics) — the technique NOAA/ECMWF use
operationally. For each `(city, lead_time, season)` build the historical distribution of
forecast error from the Open-Meteo **archive** (free, reproducible, back to 1940), then shift
and/or spread-inflate the ensemble members before computing `raw_p`.

### 1.1 `scripts/build_historical_skill.py`
- For each of the ~14–17 cities, query the archive ensemble for 1–2 years of daily values per
  metric and lead time (`days_ahead = 1..5`).
- Compute per `(city, lead_day, month)`: `mean_error` and `std_error`
  where `error = forecast − actual`.
- Store `logs/historical_skill.json`:
  `{city_key: {lead_day: {month: {mean_error, std_error, n}}}}`.
- One-time cost ≈ 14 cities × 5 leads × ~2 yr; cache locally, refresh monthly.

### 1.2 `HistoricalSkillCorrector` in `weather/probability_model.py`
```python
class HistoricalSkillCorrector:
    def adjust_members(self, members, city_key, lead_day, month):
        stats = self._lookup(city_key, lead_day, month)   # fall back month→0, then lead→nearest
        if stats is None or stats["n"] < MIN_SKILL_OBS:
            return members
        # error = forecast − actual, so if the model runs warm (+1°C) we shift members DOWN
        return [m - stats["mean_error"] for m in members]
```
- Wire it in `compute_probability` **before** `_fraction_satisfying`/`_apply_kde`, so `raw_p`
  itself is corrected. Pass `lead_day` and `month` down from `SignalGenerator.evaluate()`.
- Optionally inflate spread by `std_error` when the live ensemble looks overconfident.
- **Gate behind backtest:** only enable per `(city, lead, season)` cells where `backtest.py`
  shows the shift improves Brier; otherwise leave members unshifted.

**Why this is the highest-value item:** it grounds the model in reproducible history instead of
the implicit assumption that "ensemble spread is all the uncertainty we have", and it provides
calibration signal immediately without waiting for live trades.

---

## Phase 2 — Fix City Bias (1 hr)

**Problem:** `city_bias.py:_load()` keeps only `reliable=1` rows, so Atlanta (+2.62°C, n=5),
Paris (−1.88, n=5), Tel Aviv (−1.58, n=6), Madrid, Toronto, Dallas are dropped — large
systematic errors that go uncorrected on ~120 trades.

**Fix — apply all biases, scaled by confidence. Avoid the double-damping trap.**
`city_bias.csv` already stores both `mean_bias_c` (raw) and `damped_bias_c` (pre-damped). Pick
**one** damping path, not both:

- **Recommended:** load the raw `mean_bias_c` and scale by sample-size confidence at read time.
  ```python
  # city_bias.py _load(): drop the reliable filter, keep n and raw mean
  self._entries.append({
      "city": row["city"], "lat": float(row["lat"]), "lon": float(row["lon"]),
      "mean_bias": float(row["mean_bias_c"]), "n": int(row["n"]),
  })

  def get_offset(self, lat, lon):
      ...                                   # nearest within 100 km as today
      if not best or best_dist >= 100:
          return 0.0
      confidence = min(best["n"] / 15.0, 1.0)   # full at n≥15, ~0.33 at n=5
      return best["mean_bias"] * confidence
  ```
  Do **not** also multiply by `damped_bias_c` — that damps twice. (Atlanta: 2.622 × 0.33 ≈ 0.87,
  not 0.656 × 0.33.)
- Add a floor so a single noisy observation can't move a threshold: skip if `n < 3`.

**Validate with `backtest.py`** on the affected cities before keeping.

**Impact:** Atlanta, Paris, Tel Aviv, Madrid, Toronto, Dallas get corrections applied
(~120 trades), with weight proportional to evidence.

---

## Phase 3 — Seasonal Bias Correction (3–4 hr)

**Problem:** one flat offset per city averages over seasons; a city can run warm in August and
cold in December.

### 3.1 Extend `scripts/city_bias_report.py` + `logs/city_bias.csv` with a `month` column
Group observations by `(city, month)`; `month=0` is the all-season fallback row.
```
city,lat,lon,month,n,mean_bias_c,damped_bias_c,reliable
NYC,40.71,-74.01,0,13,1.047,0.681,1     ← all-season fallback
NYC,40.71,-74.01,7,5,2.10,1.26,0        ← July-specific
```

### 3.2 `CityBiasCorrector.get_offset(lat, lon, month=0)`
Fallback chain: exact city+month → city+month=0 → nearest city (month=0) → 0.0 if none <100 km.
Keep the same confidence scaling from Phase 2. `SignalGenerator.evaluate()` passes
`market.resolution_date.month`.

**Data note:** ~5–10 obs per city×month is the target; most cells stay sparse for months, so the
`month=0` fallback carries the load early. Don't over-promise timeline here.

---

## Phase 4 — Model Skill Weighting (2–3 hr)

**Problem:** GFS, ICON, ECMWF are equal-weighted in the fraction count; ECMWF is the most
skillful global model at 3–7 day leads, so equal weighting dilutes its signal.

**Now feasible empirically:** Phase 0 persists `model_breakdown_json` per trade, so per-model
forecasts accumulate going forward and can be scored.

### 4.1 `scripts/model_skill_tracker.py`
Read resolved trades, parse `model_breakdown_json`, compute Brier per model per city/season.

### 4.2 Weighted fraction in `ProbabilityModel`
```python
def _weighted_fraction(member_arrays, weights, threshold, direction, threshold_high):
    weighted_p = total_w = 0.0
    for model, members in member_arrays.items():
        w = weights.get(model, 1.0)
        weighted_p += w * _fraction_satisfying(members, threshold, direction, threshold_high)
        total_w += w
    return weighted_p / total_w if total_w else 0.5
```
**Start with a labeled literature prior** (clearly a prior, not fitted): ECMWF 1.5, ICON 1.1,
GFS 1.0. Replace with `model_skill_tracker.py` weights once each model has enough scored trades.
Validate with `backtest.py`.

**Impact:** raises ECMWF's effective weight from ~41% to ~48% of signal; marginal but compounds.

---

## Phase 5 — KDE Bandwidth (CONDITIONAL — only if MOS + calibration leave residual, 2 hr)

This was Phase 1.3 in the old plan. It is **demoted and gated** for two reasons:

1. **Direction isn't guaranteed.** Halving the bandwidth does *not* monotonically lower
   P(exact). For a threshold near the ensemble mode, a tighter kernel raises peak density →
   *higher* P(exact); it only lowers P in the tails. So "tighter bw fixes YES overestimation"
   only holds when the exact bin sits off-mode.
2. **It fights the calibrator.** Systematic equal-direction overestimation is exactly what the
   per-direction `equal` calibrator removes — once Phase 0 feeds it the right signal. Manual
   bandwidth tuning on top double-corrects.

**Only do this if,** after Phase 0 (clean calibrator) + Phase 1 (MOS), `backtest.py` still shows
the `equal` split overestimating. If so:
```python
def _apply_kde(members, threshold, direction, threshold_high, fallback):
    bw = "scott"
    if direction == "equal":
        n = len(members); std = float(np.std(members))
        bw = EQUAL_KDE_BW_FACTOR * std * n**(-0.2)   # tune EQUAL_KDE_BW_FACTOR via backtest
    kde = gaussian_kde(members, bw_method=bw)
    ...
```
Treat `EQUAL_KDE_BW_FACTOR` (new constant in `config.py`) as a hyperparameter swept by
`backtest.py`, not a hardcoded 0.4.

---

## Phase 6 — New Market Types (2–3 hr)

Once accuracy is up, expand the scanner:
- **6.1 Precipitation** — non-Gaussian (many zero days); two-stage `P(any rain) × P(amount|rain>0)`.
- **6.2 Wind speed** — `windspeed_10m_max` already in `METRIC_DAILY_PARAMS`; same fraction approach.
- **6.3 Overnight low** — enter ~24 h earlier where ensemble uncertainty is lower.

---

## Implementation Order (Recommended)

| Priority | Phase | Effort | Why here |
|----------|-------|--------|----------|
| 1 | 0  Stop calibration leak + persist raw_p/breakdown | 1 session | Everything else is untrustworthy until fixed |
| 2 | 0.5 Backtest harness | 1 session | Can't measure any later "+X% Brier" without it |
| 3 | 1  Historical MOS correction | 4–6 hr | Biggest win; archive-sourced, no wait on live trades |
| 4 | 2  Fix city bias (confidence-scaled, no double-damp) | 1 hr | Corrects ~120 trades; cheap |
| 5 | 3  Seasonal bias | 3–4 hr | Removes seasonal drift in the flat offset |
| 6 | 4  Model skill weighting | 2–3 hr | Now measurable via persisted breakdown |
| 7 | 5  KDE bandwidth (conditional) | 2 hr | Only if residual equal-bias remains after 0+1 |
| 8 | 6  New market types | 2–3 hr | More volume at similar edge |

---

## What This Will NOT Do

- Instantly activate Isotonic calibration — historical `raw_p` is unrecoverable; calibration
  restarts from clean data (Phase 0) plus archive MOS (Phase 1).
- Eliminate ensemble uncertainty at day 4–5 — the atmosphere is chaotic.
- Fix markets where Polymarket's crowd has near-real-time station data (Gate 9.5).
- Make YES bets on exact-hit markets consistently profitable — that market is efficient.

---

## Key Files to Touch

| File | Phase |
|------|-------|
| `weather/models.py` | 0 (PaperTrade fields) |
| `weather/paper_trader.py` | 0 (CSV headers, log raw_p not model_p) |
| `weather/probability_model.py` | 1 (MOS), 4 (weighting), 5 (KDE) |
| `weather/city_bias.py` | 2, 3 |
| `weather/signal_generator.py` | 1 (pass lead/month), 3 |
| `weather/config.py` | 1 (MIN_SKILL_OBS), 5 (EQUAL_KDE_BW_FACTOR) |
| `scripts/city_bias_report.py` | 2, 3 |
| `logs/calibration_log.csv` | 0 (move poisoned file aside) |
| `logs/city_bias.csv` | 3 (month column) |
| New: `scripts/backtest.py` | 0.5 |
| New: `scripts/build_historical_skill.py` + `logs/historical_skill.json` | 1 |
| New: `scripts/model_skill_tracker.py` | 4 |

---

## Context for Fresh Session

Read these first (in order):
1. `weather/probability_model.py` — core model (note: calibrator is applied to `raw_p`, line 75)
2. `weather/signal_generator.py` — lines 117–123: `model_p` = calibrated **and** shrunk
3. `weather/paper_trader.py` — line 232: `log_observation` currently logs the wrong quantity
4. `weather/models.py` — `RawProbabilityResult` (has `raw_p`, `model_breakdown`), `PaperTrade`, `Signal`
5. `weather/city_bias.py` — `_load()` drops `reliable=0` rows
6. `weather/config.py` — thresholds
7. `logs/city_bias.csv` — current bias data (Atlanta/Paris/Tel Aviv have reliable=0)
8. `logs/calibration_log.csv` — 35 rows, **poisoned** (calibrated values, not raw) — see Phase 0
9. `logs/paper_trades.csv` — 355 resolved trades (64.8% WR, 37% ROI); no `raw_p` column yet

**Invariants to preserve:**
- Gates 9.5, 9.6, 9.7 in `signal_generator.py` are data-driven blocks — keep them until the
  model demonstrably improves P(exact hit) accuracy in `backtest.py`. Validate, don't delete.
- Keep `model_p` (calibrated+shrunk) as the PnL/Brier scoring quantity. Phase 0 only changes
  what feeds the **calibrator** (`raw_p`), not how trades are scored.
- Do the phases in order. Phase 0 and 0.5 are prerequisites for everything after them.
</content>
</invoke>
