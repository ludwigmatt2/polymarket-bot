# Phase 3 — Station-calibrated forecasts (DWD MOSMIX), globally

**Status:** planned · **Created:** 2026-09-28 · **Branch:** `feat/mosmix-station-forecast` off `main` (`4cabed8`)

**Supersedes** `archive/PHASE1_STATION_MOS_PLAN.md` (Workstream A, never started) and closes out
`archive/PHASE2_PRICE_FLOOR_PLAN.md`. Both are archived with banners and should not be worked from.

---

## The one-line thesis

The bot forecasts an **Open-Meteo grid cell** and the market resolves on an **airport
thermometer**. Every attempt to bridge that gap with our own statistics has failed to
measurably help. DWD MOSMIX is a free, global, already-computed statistical forecast **for
that exact thermometer**, and it ships its own uncertainty. Use it.

This is the only item on the roadmap that adds *new information* rather than re-processing
what we already have.

## Why now — what the record actually says

Two weeks of clean post-reseed shadow data (Sep 15–28) plus a full-era re-read:

**1. The edge is small, real-ish, and unstable.** Tradeable band, weekly edge vs market:
`+.061, +.157, +.127, +.034, +.043, −.068, −.035, +.142`. Full gate era: **n=305,
edge +0.059, z=+1.65** after correcting for 1.61× within-day correlation. Positive on
average, not established, and it swings by more than its own mean week to week.

> Correction to the Sep 22 read, which called this a monotone decay to zero. Week 39 came in
> at +0.142 and broke that. **Any 2-week window of this series can show anything** — which is
> itself the most important operational fact in this document. Do not make decisions on
> fortnightly reads.

**2. The price floor was fitted on a relationship that reversed out of sample.**

| band | pre-floor (the data it was fitted on) | post-floor (out of sample) |
|---|---|---|
| < $0.40 | −0.046 | **+0.116** |
| ≥ $0.40 | +0.059 | +0.060 |

Full era: `< 0.40` → edge +0.033, z=+0.60. `≥ 0.40` → +0.059, z=+1.65. **Neither band shows
significant edge**, and the cheap band flipped sign across the fitting boundary. The ≥0.40
band is at least *stable* across both halves, which is the only reassuring number here.

This is the audit's overfitting finding, now demonstrated out of sample on our own rule. The
conclusion is not "remove the floor" — it is **stop fitting band rules entirely** and get a
real signal.

**3. MOS contributes nothing, three ways.** `mos_off` scores identically to `prod_mirror`
again (PF 1.88 vs 1.90, Brier 0.2362 vs 0.2352). Offline, the seasonal table beats flat bias
by only +0.5% on max-temp, below its own 1.0% ship bar. Our MOS is trained against **ERA5
grid** reanalysis, so it is structurally blind to the grid→station gap it exists to close.
MOSMIX replaces it with a correction trained on the station itself.

## What MOSMIX is, verified

Not from docs — I downloaded and parsed real files on 2026-09-22.

- **5,649 stations**, 1,556 with ICAO codes. Statistically downscaled from global models
  against each station's own observation history.
- **18 of our 19 stations covered.** WMO ids for direct use:

  | ICAO | WMO | | ICAO | WMO | | ICAO | WMO |
  |---|---|---|---|---|---|---|---|
  | KLGA | 72503 | | RKSI | 47113 | | LLBG | 40180 |
  | KATL | 72219 | | LEMD | 08221 | | CYYZ | 71624 |
  | KDFW | 72259 | | EGLC | P0478 | | ZSPD | 58362 |
  | KMIA | 72202 | | EGLL | 03772 | | VHHH | 45007 |
  | LFPB | 07150 | | RJTT | 47671 | | WSSS | 48698 |
  | LFPG | 07157 | | ZBAA | 54511 | | ZGSZ | 59493 |

  **`KDAL` (Dallas Love Field) is NOT in MOSMIX** — and it is our 3rd-highest-volume station
  (48 trades). See T6.
- **It ships uncertainty.** `TTT` (hourly temperature, Kelvin) *and* `E_TTT` (standard
  error), both fully populated: σ 0.50 °C at day 1 → ~1.4 mean → 3.2 at day 10. A per-hour
  Gaussian per station. This is what makes it a drop-in for a bucket-probability model.
- `MOSMIX_L`: 114 params, 4 runs/day (03/09/15/21 UTC), 240 h horizon, hourly steps, ~10 KB
  KMZ per station. `MOSMIX_S`: 40 params, hourly runs.
- URL: `https://opendata.dwd.de/weather/local_forecasts/mos/MOSMIX_L/single_stations/<WMO>/kml/MOSMIX_L_LATEST_<WMO>.kmz`
- **Gotcha:** `TX`/`TN` (12 h max/min) are populated for international stations but **empty
  for US ones** (KLGA had zero). Derive daily extremes from hourly `TTT`.

## Task list

### T0 — MOSMIX client *(small, no model change)*

`weather/mosmix_client.py`, mirroring `iem_client.py` in shape (module-level registry, plain
`urllib`, polite rate limit, no new dependencies — KMZ is a zip of XML, so `zipfile` +
`xml.etree` suffice).

```python
def fetch_station(wmo: str) -> MosmixForecast | None   # parse latest KMZ
def daily_extreme(fc, day: date, tz: str, kind: str) -> tuple[float, float] | None
    # -> (mean_degC, sigma_degC) for that station-local day, from hourly TTT/E_TTT
```

- Add `wmo` as a 6th field to `iem_client._STATION_REGISTRY`, so ICAO→WMO lives next to the
  ICAO→ASOS mapping already there. One source of truth for station identity.
- **Disk cache keyed on (wmo, issue time)** under `DATA_DIR/cache/mosmix/`. A run is valid
  6 hours; the scan runs hourly, so this cuts fetches ~6× and makes the scan resilient to
  DWD being down. DWD states no availability guarantee — treat a miss as "feature stands
  down", never as an error.
- σ for a daily extreme is **not** the hourly σ. Combining hours is not independent, and the
  max of hourly means understates the true daily max. Start with
  `sigma_day = max(E_TTT over the day's hours)` and record the residual bias in T3 rather
  than guessing a correction now.

Tests: parse a committed fixture KMZ (US station with empty TX, international with populated
TX), missing-station → `None`, stale-cache eviction, and that a DWD outage returns `None`
rather than raising.

### T1 — Data-quality audit: does MOSMIX actually beat our ensemble at the station? *(the go/no-go)*

**Fire this before writing any model code.** Offline, no trading:
`scripts/mosmix_backcheck.py`.

For each registered station, over as many past days as the archive allows, compare against
**IEM station truth** (`iem_client.daily_maxmin`, the same source that settles):

- MAE / bias of `MOSMIX daily extreme` vs `Open-Meteo ensemble mean` vs `our current
  MOS-corrected ensemble mean`.
- Is `E_TTT` honest? Compute the PIT/z-score histogram of
  `(truth − TTT_mean) / sigma_day`. If σ is systematically too small, every downstream
  probability will be overconfident — and that is the exact failure mode that has cost us
  money twice already.
- Per station, since international IEM values are a recompute and may not match Wunderground.

**Gate:** proceed only if MOSMIX beats the current pipeline on station MAE **and** its σ is
within ~20% of honest, on a majority of stations. If MOSMIX is merely *equal*, stop — the
whole thesis was that it carries information we lack.

**Do not skip this.** `archive/PHASE1_STATION_MOS_PLAN.md` had the equivalent audit as its T1 and it
was never fired; that plan then sat unstarted for a month.

### T2 — Wire it as a shadow challenger *(small)*

Add to `ModelSpec` (`weather/shadow.py`) a `station_forecast: str | None = None` knob and one
spec: `ModelSpec("mosmix", station_forecast="mosmix")`. No production path changes.

Integration mechanism — **anchor, don't replace.** The existing
`HistoricalSkillCorrector` already shifts members inside `compute_probability`, so the
cleanest hook is the same place:

1. **Mean-shift (start here).** Shift every ensemble member so the pooled mean equals the
   MOSMIX daily-extreme mean, keeping the ensemble's own spread. Smallest possible change,
   reuses the entire downstream pipeline (rounding pre-image, KDE, calibration), and is
   directly comparable to the existing MOS shift it replaces.
2. **Inverse-variance blend (T5, only if 1 works).** Combine the ensemble distribution and
   `N(TTT, sigma_day)` weighted by `1/σ²`. Principled rather than hand-tuned.

Keep MOS off in this spec (`mos_enabled=False`) — MOSMIX *is* the MOS, and stacking both
double-corrects. That also makes `mosmix` vs `mos_off` a clean read on whether station
calibration beats no calibration.

### T3 — Validate forward, on a horizon that can actually resolve it *(elapsed-time-bound)*

`scripts/compare_tracks.py` against `prod_mirror` — which is now a **trustworthy baseline**
(mean |Δ model_p| 0.0044, direction agreement 40/40 post-reseed).

**Promotion gate, fixed in advance:**
- `mosmix` model Brier < **market** Brier, out-of-sample, **and**
- beats `prod_mirror` on Brier at comparable n, **and**
- **n ≥ 250 resolved signals or 6 weeks, whichever is later.**

That last clause is the lesson of this document. At n=56 (two weeks) the edge series swings
±10pp. A 2-week read cannot distinguish a real improvement from noise, and three separate
decisions have already been corrupted by reading short windows (see
`archive/PHASE2_PRICE_FLOOR_PLAN.md` "Finding 6").

**Report every result segmented by price band.** Unsegmented reads are what hid the
cheap-longshot leak for two months.

### T4 — Corroboration gating *(the structural fix; depends on T1–T3)*

This is the change with the most upside and it is impossible without T0.

Today: `direction = sign(model_p − market_p)`, gated on `|edge| ≥ 0.11`. **The bot trades
where it disagrees most with the crowd** — which, if the model carries noise and the market
is roughly efficient, selects for maximum model error. That is adverse selection by
construction, and it is the best explanation for an edge that is positive on average but
unstable.

Replace magnitude-of-disagreement with **agreement between two independent station-aware
sources**: trade only when the Open-Meteo ensemble *and* MOSMIX both land on the same side of
the market price, and size on their agreement rather than on the size of the gap.

Ship as its own shadow spec (`ModelSpec("mosmix_corroborated", …)`) so T3's gate applies to
it separately. Expect **far fewer signals** — that is the point.

### T5 — Promote, and retire what it replaces *(small)*

On passing T3: point production at the MOSMIX path, and in the same change
**set `MOS_ENABLED = False`** — three independent measurements now say the ERA5-trained
table contributes nothing, and keeping a dead layer alive is how the Sep-20 station-table
incident happened. Keep `build_historical_skill.py` and the weekly timer for one month in
case of rollback, then delete.

Kill switch: `STATION_FORECAST_SOURCE` env, unset → current behaviour verbatim.

### T6 — Close the KDAL gap *(small, do alongside T1)*

`KDAL` (Dallas Love Field) is not in MOSMIX and is our 3rd-highest-volume station. Options,
in order of preference:

1. Use `KDFW` MOSMIX as a proxy and **measure the KDAL−KDFW bias** against IEM truth for both
   (they are ~20 km apart). If the bias is stable and small, apply it as a fixed offset.
2. If the bias is unstable, add `temperature_2m_*@KDAL` to a per-station exclusion and stop
   trading Dallas. It is 48 trades of unknown edge; dropping it costs little.

**Do not** silently fall back to the grid ensemble for KDAL only — that reintroduces the exact
inconsistency this phase exists to remove, in the one market where we would not notice.

### T7 — Fix `iem_client.mos_forecast()` *(trivial, independent)*

It has been returning `[]` for every call. IEM now requires timezone-aware timestamps; the
client sends naive ones and `except Exception: return []` swallows the 422. Append `Z` (or
send `%Y-%m-%dT%H:%M:%SZ`). Verified working once corrected:
`KLGA MEX n_x=68, KATL n_x=90, KMIA n_x=86`.

Worth doing regardless of MOSMIX: NWS MOS is a genuinely independent US cross-check and the
natural second source for T4 on US stations. Also **narrow that bare `except`** — a silent
swallow hid a dead integration for weeks.

## Explicitly NOT in this plan

- **NBM text bulletins.** NOAA's own docs: they are generated from the nearest grid point and
  are "NOT calibrated to ground station observations, which is different from the MOS text
  bulletins." NBM would reproduce our defect with extra steps.
- **Paid APIs, for now.** Meteomatics (`metar_<ICAO>`, `source=mm-mos`, 15-day, 30-min
  refresh, 1,600+ airport MOS stations) is the right commercial option and its 30-minute
  refresh genuinely beats MOSMIX's 6-hourly for event-day trading. The Weather Company / WU
  (from $500/mo) is the source Polymarket actually resolves on, so it carries zero
  source-mismatch risk. **Revisit only after T3 passes** — buying data to feed a strategy with
  unproven edge is the wrong order.
- **More probability-model work.** EMOS, recency calibration and λ tuning have all been
  measured across two weeks of clean shadow data. `lambda_1_0` is the worst track
  (PF 0.77); `recency_cal` merely matches production. The estimator is not the problem.
- **New band/price/city exclusion rules.** See the out-of-sample reversal above.
  `BLOCKED_YES_CITIES = ["Tokyo"]` should eventually be re-tested as a shadow spec, not
  extended.

## Risk register

| Risk | Mitigation |
|---|---|
| `E_TTT` is a climatological error, not flow-dependent spread — understates uncertainty on volatile days | T1 measures PIT honesty; if σ is optimistic, inflate it per (station, lead) before trusting probabilities |
| DWD has no SLA | 6-hour disk cache; missing data = feature stands down, never an exception |
| DWD licence not stated inline (points to a price list + conditions doc) | **Confirm commercial use with `opendata@dwd.de` before T5 promotion.** Free for T0–T3 research either way |
| Max of hourly MOS understates a true daily max | Measure the residual in T1 and correct explicitly; do not hand-wave it |
| MOSMIX is one provider — swapping Open-Meteo for it trades one monoculture for another | T4's corroboration design keeps both, and disagreement becomes a *reason not to trade* |
| We overfit MOSMIX the way we overfit the floor | T3's n≥250 / 6-week gate, fixed before any data is seen |

## Sequencing

T0 + T7 in one session (both self-contained). **T1 is the go/no-go and gates everything
after it** — if MOSMIX does not beat our pipeline at the station, stop and say so. T2 is small.
T3 is elapsed time, ~6 weeks. T4 after T3 passes. T5/T6 last.

Keep the bot live at ~$3 positions throughout: it is cheap, and the forward data is the point.

## Decision criterion, set in advance

**If a MOSMIX-fed track does not beat the market's Brier out-of-sample over 6+ weeks, the
honest conclusion is that this strategy has no durable edge**, and the remaining options are
market-making (capturing spread instead of paying it) or shutting down. Write that conclusion
down now, while nothing is invested in it.
