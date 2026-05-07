# Weather Probability Model — Improvement Research

**Goal:** improve calibration and Brier skill for binary weather markets (single-day and monthly aggregates of temperature, precipitation, wind).

**Current pipeline (baseline):** raw ensemble fraction → Gaussian KDE smoothing → isotonic regression (after 50+ obs) → quality gates on member count and cross-model spread. Monthly aggregates use `historical_avg_full_month / historical_avg_first_7_days` as a multiplicative ratio on each member's 7-day sum.

**Known weakness:** the monthly multiplicative scaling collapses when the first 7 days happen to be drier than the historical first-7-days average (Seoul May 2026: 24 mm projected vs ~100 mm climatology). This is a structural flaw — the model has no climatology prior, only a climatology ratio.

---

## 1. Statistical Post-Processing of Ensembles

### 1.1 MOS (Model Output Statistics)
**What:** linear regression `actual = a + b·forecast` on historical (forecast, observed) pairs, per location/season/lead-time.
**Why for us:** GFS has known warm bias; ICON has known wet bias in convective regimes. MOS removes them per-location.
**Cost:** 1 week (need backfilled forecast archive + actuals).
**Data:** 50–200 paired (forecast, actual) per location/metric.
**Impact:** 10–30% MAE reduction for temperature; less for precip.
**Recommendation:** SKIP — superseded by EMOS, which works on the full distribution.

### 1.2 EMOS / NGR (Non-homogeneous Gaussian Regression)
**What:** fit a parametric predictive distribution where mean and variance are linear functions of ensemble mean and ensemble spread. Gaussian for temp/wind, censored-shifted-Gamma or Bernoulli-Gamma for precipitation. Gneiting et al. 2005.
**Why for us:** directly replaces raw member-counting + KDE with a smooth calibrated CDF, so `P(T > 90°F)` is a clean integral. Auto-corrects spread underdispersion (a known GFS/ICON pathology). Removes the noisiness of counting 70 members at the threshold.
**Cost:** 1–2 weeks. Libraries: `properscoring`, `crch` (R), `pyEMOS`. One implementation per metric type (Gaussian vs Gamma).
**Data:** 50–200 paired forecasts/actuals per location/metric. Pool across nearby locations to bootstrap.
**Impact:** typically 15–25% CRPS improvement over raw ensemble; similar Brier improvement at threshold-based events.
**Recommendation:** STRONG YES. Single highest-leverage upgrade.

### 1.3 BMA (Bayesian Model Averaging)
**What:** weighted mixture of per-model predictive PDFs; weights via EM on log-likelihood. Raftery et al. 2005.
**Why for us:** GFS and ICON have different regional skill; current code weights them by member count (44%/56%) regardless of skill.
**Cost:** 1 week on top of EMOS.
**Data:** 200+ paired obs to fit weights stably; ideally per location/season.
**Impact:** 2–5% CRPS improvement over EMOS alone.
**Recommendation:** DEFER. EMOS gets ~80% of the benefit.

### 1.4 Quantile Mapping
**What:** non-parametric — map every forecast quantile to the historical observed quantile at that rank.
**Why for us:** distribution-free; helpful for precipitation extremes, where ensembles systematically under-forecast tails.
**Cost:** 2–3 days.
**Data:** 100+ paired obs per location/metric.
**Impact:** 5–15% CRPS gain, larger for precipitation extremes.
**Recommendation:** YES as a fallback / complement to EMOS for precipitation specifically.

---

## 2. Calibration Alternatives to Isotonic Regression

| Method | Pros | Cons | Min N |
|---|---|---|---|
| Isotonic (current) | Non-parametric, monotone | Overfits at small N, step function | 100+ |
| Platt scaling | 2 params, low variance | Assumes sigmoid | 30+ |
| Beta calibration | 3 params, more flexible than Platt | Slightly heavier | 50+ |
| Temperature scaling | 1 param, very stable | Only rescales confidence, can't fix shape | 30+ |

**Current 50-obs threshold for isotonic is too low** — at N=50 isotonic over-fits and creates pathological step functions.

**Three-stage plan:**
- N < 30: no calibration (raw EMOS-derived P).
- 30 ≤ N < 200: **beta calibration** (Kull et al. 2017). Three params, well-behaved, handles over- and under-confidence.
- N ≥ 200: isotonic with cross-validated splits.

**Reliability diagrams + ECE:** add a diagnostic CLI command. 1 day of work, essential for *seeing* whether calibration is helping.

**Cost:** ~3 days for switch + diagnostics. **Impact:** 5–10% Brier improvement in the small-N regime where we currently live.
**Recommendation:** YES, do this Week 1, independent of EMOS.

---

## 3. Climatology Priors for Long-Horizon Forecasts (THE CORE FIX)

Forecast skill decays with lead time. By day 7, ensemble precip skill is barely better than climatology; by day 14, it's often worse. Operational forecasting handles this with **lead-time-dependent blending**:

```
P_blended(t) = w(t) · P_forecast(t) + (1 - w(t)) · P_climatology
```

`w(t)` decays from ~1.0 at lead-time 1 to near 0 by lead-time 14+. Tuned by minimizing CRPS on the historical archive.

### For monthly aggregates specifically

Current model effectively uses `w = 1` and treats climatology only as a multiplicative ratio. The fix is **additive blending in distribution space**:

```
member_projected_total = sum(member, days 1–7) + sample(climatology_distribution_for_days_8–end)
```

This is mathematically a Bayesian update: prior = climatology of monthly totals, likelihood = first-7-days forecast, posterior = first-7-days actual + draws from conditional climatology for days 8–N. Operationally it's just a per-member resample.

**Cost:** 3–4 days. Need 20–30 years of daily archive (Open-Meteo archive endpoint).
**Impact:** largest single fix for monthly markets — should eliminate the systematic bias in the Seoul-style failure mode entirely (expected error from ~75% to <20%).
**Recommendation:** **DO THIS FIRST.** Cheapest fix for the highest-symptom problem.

---

## 4. Better Monthly-Aggregate Handling

### 4.1 Decompose-and-blend (RECOMMENDED, see §3)
Replace multiplicative ratio with `monthly_total = forecast(days 1–7) + climatology_sample(days 8–end)`. Ensemble shape on days 1–7, climatological distribution on days 8+.
**Cost:** 3–4 days. **Impact:** very high. **Data:** 20+ years daily archive.

### 4.2 Sub-seasonal models (CFSv2, ECMWF S2S)
True 4–6 week probabilistic models. CFSv2 free via NOAA; ECMWF S2S via the S2S Project archive (delayed). Genuine skill on 8–30 day windows.
**Cost:** 1–2 weeks (new ingestion + parameters).
**Impact:** medium-high after integration; sub-seasonal skill is modest but real for temperature anomalies and large-scale precip.
**Recommendation:** Phase 2, after §4.1.

### 4.3 Bayesian update with first-k-days observed
Once part of the target month has happened, fold in actuals: `posterior(monthly | days_1..k_observed)`. Reduces uncertainty as month progresses, allows re-trading.
**Cost:** ~2 days, basically free after §4.1.
**Recommendation:** YES, immediate follow-up to §4.1.

---

## 5. ML Approaches

### 5.1 XGBoost / LightGBM on (ensemble_features → outcome)
Features: ensemble mean, std, quantiles, fraction-above-threshold, climatology mean/std, lead time, month, location embedding. Target: binary outcome.
**Cost:** 1–2 weeks plumbing.
**Data:** 1,000–5,000 labeled examples. **We currently have ~0.** Blocker.
**Recommendation:** **NO, not now.** Revisit in 6+ months once the calibration log has thousands of resolved markets. EMOS captures most of what an ML model would, with far less data.

### 5.2 Analog Methods
Find K most-similar historical forecasts; look at outcomes.
**Cost:** ~1 week. Needs a long *forecast* archive (10+ years), not just observations. NOAA GEFS reforecasts (20+ years) are workable but heavy.
**Impact:** modest; outperformed by EMOS in nearly all studies.
**Recommendation:** SKIP.

---

## 6. Variance / Spread-Skill Relationships

Spread-skill correlations are real but weak — Whitaker & Loughe 1998 reports ~0.3–0.5. Meaningful at the population level, but a single high-spread forecast is not necessarily wrong. **Hard rejection on spread alone wastes signal.**

Better:
1. Use spread as a **feature**, not a gate. Inflate predictive variance in EMOS based on spread (this is exactly what NGR does).
2. **Calibrate spread**: fit `actual_error_std = a + b·forecast_spread`, use calibrated spread.
3. Soft penalty in confidence score (already partly doing this) — drop the hard `MAX_ENSEMBLE_SPREAD` gate.

**Cost:** built-in once EMOS is in place. **Impact:** ~5% Brier + more tradeable signals.
**Recommendation:** YES, drop hard spread gate after EMOS deployment.

---

## 7. "NASA-level" Modeling — Reality Check

Are NASA GEOS / ECMWF IFS meaningfully better than GFS for our use case?

- **Temperature, 1–3 day lead:** ECMWF beats GFS by ~10–15% RMSE. NASA GEOS comparable to GFS or worse.
- **Precipitation:** ECMWF advantage larger, ~15–20%.
- **7-day lead and monthly aggregates:** all global models are within noise; skill dominated by climatological/sub-seasonal signal, not the deterministic core.

**Practical:**
- ECMWF ENS commercial license: €10k+/yr — not economic.
- ECMWF Open Data (HRES + 50-member ENS at 0.25°): **free**, available via Open-Meteo's `ecmwf_ifs025`. Add it.
- ML weather models (Pangu-Weather, GraphCast, Aurora): match or beat ECMWF at 1–10 days, free, single-GPU. GraphCast is available via Open-Meteo's `*_seamless` endpoints in some regions.

**The real "NASA-level" play is ML weather models, not government supercomputer models.**

**Cost:** 2–3 days to add ECMWF + GraphCast. **Impact:** 5–10% Brier at 1–3 day leads, marginal at 7+ days.
**Recommendation:** YES, add ECMWF IFS open data and GraphCast to the pool.

**Bigger lesson:** post-processing (EMOS, climatology blending) yields more skill per dollar than chasing better raw models. Well-post-processed GFS+ICON beats a raw ECMWF.

---

## Prioritized Roadmap

### Week 1 — Quick wins, no new data infrastructure
1. **Climatology blend for monthly aggregates** (§3, §4.1) — 3–4 days. Biggest single fix.
2. **Reliability diagrams + ECE diagnostic** (§2) — 1 day. Required to see whether other changes help.
3. **Soften spread-rejection gate to a feature** (§6) — half day. More tradeable signals.

### Week 2–3 — Calibration overhaul
4. **Three-stage calibration: none / beta / isotonic by N** (§2) — 3 days.
5. **Add ECMWF IFS open data + GraphCast to ensemble pool** (§7) — 2–3 days.

### Week 3–6 — EMOS deployment
6. **Build forecast archive** — 3–5 days. Backfill 6+ months of paired (forecast, actual) per active location/metric.
7. **Implement EMOS / NGR per metric** (§1.2) — 1–2 weeks. Replaces raw counting + KDE with one principled step.
8. **Quantile mapping for precipitation tails** (§1.4) — 2–3 days.

### Month 3+ — Advanced
9. **Sub-seasonal models (CFSv2 / ECMWF S2S)** (§4.2) — 1–2 weeks.
10. **In-month Bayesian update with observed days** (§4.3) — 2 days, free after §4.1.

### Deferred / not worth it
- BMA (§1.3) — marginal over EMOS.
- ML model on historical (§5.1) — defer until ≥1,000 labels.
- Analog methods (§5.2) — outperformed by EMOS.
- Paid ECMWF ENS license — uneconomic.

---

## Bottom Line

Two improvements account for ~80% of available skill gain:

1. **Climatology-blended monthly aggregates** (Week 1) — fixes the only known catastrophic failure mode.
2. **EMOS post-processing** (Week 3–6) — replaces raw counting + KDE with a principled calibrated spread-aware predictive distribution.

Start with #1: cheapest fix for the highest-symptom problem. #2 is the durable platform everything else (calibration, ML features, sub-seasonal data) plugs into.
