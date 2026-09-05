# Phase 1 — Per-station min-temp bias correction

**Status:** planned (not started) · **Created:** 2026-09-03 · **Branch to build on:** `feat/shadow-model-tracks`

## Why

The Sep 3 2026 audit traced the entire live-era loss to **minimum-temperature markets**
(live record: min-temp 2/9 win / −$25.95; max-temp 39/65 / +$12.46). Excluding min-temp
flips the wallet from −14.0% to +12.9% on deposited capital. Min-temp was disabled from
live execution in `f9d7e78` (`LIVE_EXCLUDED_METRICS`), which stops the bleed but does not
fix the underlying model.

**Root cause.** Polymarket resolves each daily-temperature market off the resolving
**airport station's** finalized reading (via Weather Underground). The model forecasts the
Open-Meteo **grid** at the station's coordinates — still a grid interpolation, not
station-calibrated. Overnight lows are dominated by local siting (tarmac / urban heat /
radiative cooling), so the station min runs ~1–2 °C **warmer** than the grid min. The model
bets NO on warm min-buckets the station keeps hitting. Independent Open-Meteo archive check
on the 4 losing min bets: all came in ~1–2 °C below their buckets (model "right" on the grid)
yet all resolved YES on-chain. Max temp is far less siting-sensitive → already calibrated.

The current live MOS (`build_historical_skill.py` `build_city`) trains forecast-vs-**ERA5
grid actuals**, so it is structurally blind to the grid→station gap. Phase 1 trains the
correction against **station observations** instead.

## What already exists (do NOT rebuild)

- **Diagnosis confirmed:** `build_historical_skill.py:264` (`build_city`) scores forecast vs ERA5 grid.
- **Station-trained builder:** `build_historical_skill.py --stations` → `build_station(icao, …)` (line 281)
  corrects forecast-at-station toward the **IEM station reading**, iterating `_STATION_REGISTRY`.
- **Corrector needs ~no change:** `HistoricalSkillCorrector._nearest_city` resolves a station-keyed
  entry at ~0 km (forecasts are already at station coords), so a station table "just works" once loaded.
- **Station truth + NWS MOS clients:** `iem_client.daily_range()` (station history),
  `iem_client.mos_forecast(icao, …, "MEX")` (free NWS MOS — Phase 2 cross-check).
- **Validation tooling:** `scripts/compare_tracks.py` (forward Brier vs *market* Brier leaderboard);
  shadow harness (`weather/shadow.py` — `ModelSpec`, isolated calibrators, per-cell `DispersionCorrector`).

Estimate: **~60% built.** Phase 1 is assembly + validation + a safe re-enable path + a rebase.

## Task list (ordered)

### T0 — Rebase the branch onto main *(small; do first)*
`feat/shadow-model-tracks` is stale — it predates the archive cleanup and the min-temp filter,
and its diff *reverts* the `LIVE_EXCLUDED_METRICS` guard at `live_trader.py:432`. Rebase onto
`f9d7e78`, keep the guard, re-run the suite.

### T1 — Data-quality audit *(medium; this is the go/no-go)*
Before trusting any fit, per resolving station (especially international: RKSI/Incheon,
RJTT/Tokyo, LFPG/Paris):
- Check IEM `daily_range` coverage & agreement vs actual on-chain resolutions back to Jan 2024.
  IEM DSM is **US-only**; international is a *recompute* that can differ from the Wunderground
  value that actually settles (`iem_client.py:16`). If IEM min for Incheon disagrees with
  settlement, the training truth is itself wrong and must be sourced differently.
- Check per-`(station, min, lead, month)` cell counts vs `MIN_SKILL_OBS=30`. Min-temp buckets
  may be thin; confirm the month-0 all-season fallback carries them.

**Biggest risk to kill early.** If IEM international min readings don't match settlement, the
whole correction trains toward the wrong target — for exactly the stations that are the problem.

### T2 — Build the station skill table *(small–medium)*
`build_historical_skill.py --stations --validate-only` first (reports MAE reduction, writes
nothing); inspect min-temp `mean_error` per station (expect a systematic warm shift). Then build
`historical_skill_station.json` as a **separate file** (not merged over the city table) so it's a
clean A/B and can't silently degrade max-temp.

### T3 — Wire a `station_mos` shadow challenger *(small)*
Add `ModelSpec("station_mos", …)` in `weather/shadow.py` whose `HistoricalSkillCorrector` loads
the station table (add a `skill_path` arg to `ModelSpec.build_model`). Append to `DEFAULT_SPECS`.
No live-path change.

### T4 — Validate, min-temp-first *(medium; elapsed-time-bound)*
Let `station_mos` accrue forward on the **min-temp signals the paper track still logs** (why the
fix was live-only). Compare vs `prod_mirror` via `compare_tracks.py` on: min-temp Brier vs market
Brier, PF, and calibration (predicted vs realized YES). Cross-check US stations against
`iem_client.mos_forecast` (NWS MEX) as a free second opinion.

**Gate:** re-enable `(station, min)` **iff** `station_mos` beats the **market's** Brier on that
station's min-temp signals out-of-sample over the gate window (mirror the existing go-live gate
thresholds). Beating the crowd's price is the only bar that means real edge — and it's what
`compare_tracks.py` already measures.

### T5 — Per-(station, metric) re-enable mechanism *(small–medium)*
Generalize `LIVE_EXCLUDED_METRICS` (today global) to an exclusion keyed on `(station_icao, metric)`
— or an allowlist of station+metric pairs that passed T4 — so min-temp comes back one station at a
time as each clears the gate. Max-temp untouched. Add tests mirroring `test_excluded_metric_skips_live_order`.

### T6 — Promote to live *(small)*
Point the production `HistoricalSkillCorrector` at the station table for the passing station and flip
its `(station, min)` pair on. Watch live via the existing degradation alerts.

## Effort

T0–T3 ≈ one session (assembly). T4 is elapsed-time-bound (forward accrual). T5–T6 small.

---

# Workstream B — Automatic seasonal recalibration

**Status:** in progress (`recency_cal` shadow challenger being built) · **Added:** 2026-09-06

## Why

Sep 5–6 2026: max-temp calibration drifted as summer→autumn. Early era predicted
YES 0.35 / realized 0.33 (calibrated); late era predicted 0.37 / realized **0.54**
(under-predicting YES on the central bucket) — the same wrong-side-of-coinflip mode
as min-temp, newer and milder. On-chain confirmed; no code regression; MOS verified
loaded with full September cells. Root cause is regime shift, and three pipeline
layers don't adapt to it:

| Layer | State | Why it can't track the season |
|-------|-------|-------------------------------|
| MOS bias table | static JSON, built Jul 8, never rebuilt | frozen; month cells a year stale + grid-trained |
| Variance inflation λ | hard-coded 2.0, fit on July | fixed; autumn more variable ⇒ under-dispersed |
| Calibrator | online, refits every 10 obs, but on ALL history equally | Aug-heavy 365-obs log outvotes ~40 autumn obs ~7:1 |

## Mechanisms (ranked by leverage)

### A. Recency-weighted calibrator — `recency_cal` (BUILDING FIRST)
Weight calibration observations by age (exponential decay, ~30-day half-life) so the
current regime dominates the fit. `LogisticRegression` and `IsotonicRegression` both
accept `sample_weight`, so it's a bounded change to the fit path. Decay (not a hard
window) never starves the per-direction fits below `MIN_CALIBRATION_OBS`. The existing
`MAX_CALIBRATION_SHIFT` clamp guards a sick fit. Directly targets the observed drift and
makes the model track any future regime shift. Requires carrying the observation
timestamp (today `_calibration_obs` is `(model_p, outcome)` only; the calibration_log
CSV already has the ts column). Built as shadow spec `recency_cal`; OFF by default —
production behavior is byte-identical unless `calibration_halflife_days` is set.

### B. Scheduled MOS rebuild (systemd timer)
Weekly rerun of the skill-table build on recent data → month cells stay fresh (seasonal
for free). This is where Workstream A (Phase 1) plugs in: rebuild a STATION-trained
table so it fixes the target and the cadence together. Agent can't install timers —
ship the `.timer`/`.service` units for the user to enable.

### C. Periodic λ re-fit — offline, shadow-gated (NOT live auto-tune)
Re-fit variance inflation on a slow cadence; promote only if it beats prod on the shadow
harness. Deliberately not auto-tuned live (dispersion over-corrects easily); let A absorb
most dispersion drift and keep λ a slow validated knob.

## Guardrail
Every mechanism is a shadow challenger first; promote only when `compare_tracks.py` shows
it beats the MARKET's Brier out-of-sample. Nothing touches live money unproven. The bot
keeps trading as-is meanwhile (small stakes, test capital — user's call to keep it live).

## Endgame
Scheduled rebuild of a station-trained MOS (A/Phase 1) + recency-weighted calibrator (B/`recency_cal`)
= a model that corrects toward the right truth AND keeps up with the season, no manual step.

---

## Related

- Memory: `mintemp-structural-leak` (finding + deployed fix), `shadow_tracks_and_emos` (harness),
  `resolution_source` (station-vs-grid foundational).
- Phase 2 (later): NWS NBM/MOS station forecasts as a free US-airport second opinion / drop-in.
