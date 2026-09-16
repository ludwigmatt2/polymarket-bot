# Phase 2 — Price floor, honest baselines, and the model backlog

**Status:** planned · **Created:** 2026-09-14 · **Branch to build on:** `main` (`9bbd659`)

Supersedes nothing. Sits *alongside* `PHASE1_STATION_MOS_PLAN.md`, whose Workstream A is
still entirely unstarted and whose Workstream B is partly built. This plan orders the work
by evidence strength and cost, not by ambition.

---

## The Sep 14 read

Live era = Aug 25 → Sep 14, 83 resolved fills, $96.34 deposited.

| Scenario | n | Win% | PnL | PF | ROI/stake |
|---|---|---|---|---|---|
| **Actual** | 83 | 47.0% | **−$7.06** | 0.95 | −2.40% |
| No min-temp from day 1 | 79 | 49.4% | +$8.96 | 1.07 | +3.23% |
| No sub-$0.40 fills from day 1 | 71 | 53.5% | +$10.89 | 1.09 | +4.10% |
| **Neither** | 67 | 56.7% | **+$26.91** | 1.25 | +10.79% |

Two facts drive everything below.

**1. The current drawdown is NOT min-temp.** The min-temp guard (`f9d7e78`) works — only 4
min fills ever hit the era, all before Sep 3, none since. The Sep 10→14 slide (+$17.14 →
−$7.06) is entirely max-temp, and it is a **cheap-longshot leak**: 0-for-9 on fills under
$0.20 where the model says p≈0.29 and the market says 0.14–0.18. Paper confirms at larger n
(live-reachable `hourly`+`intraday` only, retired `scan_source="longshot"` rows excluded):

| entry_price | n | W | PnL | ROI |
|---|---|---|---|---|
| < 0.40 | 43 | 5 (11.6%) | −$246.02 | −28.5% |
| ≥ 0.40 | 204 | 120 (58.8%) | +$594.16 | +14.8% |

Same *shape* as min-temp — the model systematically overprices a region the market prices
correctly — and the same fix pattern applies.

**2. No model we have beats the market.** `compare_tracks.py`, signals since Sep 6:

```
track          n    WR    PF      PnL   mBrier  mktBrier
production    67   25%  0.56  -419.46  0.2192   0.1781
emos_percell  95   58%  0.75  -199.97  0.2042   0.1856
lambda_1_0    68   68%  1.07   +30.43  0.1816   0.1751
lambda_2_5   137   51%  0.77  -298.94  0.2241   0.1970
mos_off      111   50%  0.70  -318.11  0.2220   0.1913
prod_mirror  111   50%  0.70  -316.61  0.2218   0.1912
recency_cal  108   49%  0.67  -350.35  0.2224   0.1897
```

Every track loses to the crowd price, this week and over the full gate era. `recency_cal` —
Workstream B's flagship — is the *worst* track. `mos_off ≈ prod_mirror` to four decimals, so
the MOS table is contributing nothing. **This is the reason to plug the cheap leak first and
not start a model-architecture project this week.**

---

## Ordering principle

Evidence strength × cost. T1 is a two-hour change with a 247-trade regime break behind it.
T2–T3 are cheap and unblock every future promotion decision. T4+ is the expensive model work,
and it should not start until T2 makes its results trustworthy.

---

## T1 — Live-only price floor *(small; do first)*

Exactly the `LIVE_EXCLUDED_METRICS` pattern: stop the bleed in live, keep the paper track
logging so a real fix can be proven before re-enable.

- `weather/config.py`: add
  ```python
  # Cheap contracts are a structural loser: the ensemble tail is fatter than
  # reality, so far-from-median buckets get too much mass. Gate-era record splits
  # 5/43 (11.6%) below 0.40 vs 120/204 (58.8%) at or above. Live-only — the paper
  # track keeps logging so a distribution fix can be validated before re-enable.
  # Kill switch: set to 0.0.
  LIVE_MIN_ENTRY_PRICE = 0.40
  ```
- `weather/live_trader.py`, in `execute_signal` immediately after the
  `LIVE_EXCLUDED_METRICS` block (line ~462):
  ```python
  if signal.entry_price < LIVE_MIN_ENTRY_PRICE:
      self._log_skip(signal, f"below_price_floor:{signal.entry_price:.3f}")
      return None
  ```
  Gate on `entry_price` (the mid), not the fill — era mean slippage entry→fill is 0.0059
  (max 0.02), so the mid errs conservative and is available pre-trade.
- Tests in `tests/test_live_trader.py`, mirroring `test_excluded_metric_skips_live_order`:
  a sub-floor signal places no order and logs `below_price_floor`; an at-floor signal in the
  same scan still trades; `LIVE_MIN_ENTRY_PRICE = 0.0` disables the guard entirely.
- **Do not tune past 0.40.** Floors of 0.45+ score better on the 83-trade live era (PF 1.46
  vs 1.31) but that is fitting noise. 0.40 is where the data changes regime. Any tighter
  floor must come from a shadow track, not a backtest sweep.

**Expected effect:** era counterfactual +$10.89 alone, +$26.91 stacked with the min-temp
guard already in place.

## T2 — Make `prod_mirror` a real baseline *(small; blocks every promotion)*

`prod_mirror` is supposed to be production. It isn't: on the 45 markets both touched, mean
|Δ model_p| = 0.061 (max 0.259); it trades 66 markets production never saw and misses 22 it
did. Cause is its isolated calibrator starting cold on Sep 6 against production's 365-obs log.

Until this is fixed, challenger-vs-`prod_mirror` is valid but challenger-vs-production is
not — which means **no shadow result can currently justify a live promotion**.

- Seed each shadow calibrator from the production `calibration_log.csv` at track creation
  (`weather/shadow.py`, `ShadowTracker.__init__` / `ModelSpec.build_model`), rather than
  starting empty. `recency_cal` re-weights the same seeded history by age, so it still
  expresses its hypothesis.
- Add a faithfulness assertion to `scripts/compare_tracks.py`: report mean |Δ model_p| and
  direction-agreement between `production` and `prod_mirror` on shared markets, and print a
  loud warning above the leaderboard when mean |Δp| > 0.02. A silently-drifting baseline
  should never again be discovered by hand.
- Re-seeding restarts the forward clock for the tracks. Accept that — the current numbers
  are not decision-grade anyway.

## T3 — Fix the `compare_tracks.py` footgun *(tiny)*

`DATA_DIR` falls back to the repo root unless `RAILWAY_VOLUME_MOUNT_PATH` is set (the systemd
unit sets it via `EnvironmentFile`; a manual `sudo -u bot venv/bin/python` does not). The
script then prints "no resolved trades yet" instead of erroring — it silently reads
`/opt/polymarket-bot/logs` instead of `/opt/polymarket-bot/data/logs`.

Make `--log-dir` resolution explicit: if the resolved directory contains no
`paper_trades.csv`, exit non-zero naming the path it tried. Same check for the `shadow/`
subdir. Cheap, and it prevents a wrong "nothing is running" conclusion.

## T4 — Decide `lambda_1_0` on honest data *(elapsed-time-bound)*

`lambda_1_0` (variance inflation OFF, vs production's λ=2.0) is the only hypothesis with
signal: best model Brier (0.1816), only positive PnL, best win rate. It still loses to the
market (0.1751), so it does **not** clear the existing promotion bar.

After T1 and T2 land, let it accrue on re-seeded calibrators and re-read. Promote to live
iff it beats the MARKET's Brier out-of-sample over the gate window. If it beats production
but still loses to the market, that is an argument for tightening the edge floor, not for
promoting the model — log the decision either way.

Note the interaction: variance inflation is what fattens the tails, and fat tails are the
suspected cause of the T1 leak. `lambda_1_0` winning would be *consistent* with the
longshot-leak diagnosis and might make the price floor partly redundant later. Do not
pre-empt that — keep the floor until a track proves otherwise.

## T5 — Kill or keep `recency_cal` *(small, decision only)*

Workstream B's flagship is the worst track on every axis. Either the seasonal-drift theory is
wrong, or the cold-calibrator artifact (T2) is masking it — the 30-day half-life is
meaningless on a log that only started Sep 6. **Re-judge only after T2.** If it still trails
`prod_mirror` at comparable n on seeded calibrators, retire the spec and strike Workstream B
mechanism A from the plan rather than leaving it running as noise.

## T6 — Investigate the tail directly *(DONE — and it refuted the premise above)*

**Built:** `scripts/calibration_by_price.py`, which buckets resolved signals on the cost of
the contract actually bought and compares three numbers on identical events: what the model
claimed, what the market claimed, and what happened.

**The "fat tails" hypothesis this task was written on is WRONG.** Cutting by |model_p − 0.5|
— the tail-ness cut the plan asked for — shows overconfidence that is essentially *flat*, not
tail-shaped (gate era, n=281):

```
|model_p-0.5|      n   model  market  realized  mdl err  mkt err
  0.00-0.10       55   0.560   0.436     0.473   -0.088   +0.037
  0.10-0.20      143   0.647   0.518     0.545   -0.101   +0.027
  0.20-0.30       83   0.534   0.409     0.458   -0.076   +0.049
```

The model is overconfident by ~0.09 everywhere. A tail-shrink term would be fitting a shape
that isn't there. **Do not build one.**

Cut by contract cost instead and the real structure appears:

```
entry_price        n   model  market  realized  mdl err  mkt err  read
  0.10-0.20       37   0.280   0.160     0.108   -0.172   -0.052  STAY OUT — price biased too
  0.30-0.40        8   0.493   0.369     0.125   -0.368   -0.244  STAY OUT — price biased too
  0.40-0.50       75   0.590   0.460     0.600   +0.010   +0.140  real edge over market
  0.50-0.60      141   0.667   0.539     0.546   -0.121   +0.007  model overconfident
  0.60-0.70       20   0.747   0.627     0.750   +0.003   +0.123  real edge over market
```

Below $0.40 the **market is wrong too** (−0.052, −0.244): realized 0.111 against a price of
0.198. That is the classic favourite-longshot bias, and it means the cheap band is not a
model bug to fix — it is a region where no model earns money, because the price is already
above the truth. **T1's floor is therefore the right permanent answer there, not a tourniquet.**
Above $0.40 the model clears the market by ~+6pp, which is the entire live edge.

The remaining model defect is a flat ~0.09 overconfidence in the tradeable band. That is a
calibration problem, not a dispersion one — which makes it T2/T5's territory (the calibrator
fits all history equally), not a new λ or tail term. **Re-judge after T2 re-seeds.**

### MOS is not earning its place *(second T6 finding)*

`mos_off ≈ prod_mirror` to four decimals in the shadow tracks. Chasing it found two things:

1. **The rebuild was impossible to run.** `build_historical_skill.py` resolved its city-list
   input from a bare relative `Path("logs/city_bias.csv")` while writing its output under
   `DATA_DIR`. On the VPS those are different directories, so every rebuild died with
   "city_bias.csv not found". That is why the table is still the one built 2026-07-08.
   **Fixed** — both sides now resolve under `DATA_DIR`.
2. **With the rebuild working, the seasonal MOS loses to a flat bias on max-temp** — the only
   metric we trade live. 10 cities, n_test=14790: raw MAE 1.319, flat 1.292, seasonal 1.297
   (−0.4% vs flat → "do NOT ship"). Min-temp does ship (+5.5%), but min-temp is excluded from
   live. So the shadow result and the offline MAE agree from two independent directions:
   **the seasonal MOS layer currently contributes nothing to live trading.**

Shipped anyway as scheduled infrastructure (`deploy/systemd/pmbot-mos-rebuild.{service,timer}`,
weekly, install by hand — the agent cannot install units), because a *fresh* table is the
precondition for judging the layer at all; the current one is a year stale in its month cells.
`--validate-only --fail-on-no-ship` gates the rebuild so a table that loses to flat bias can
never silently replace the live one, and the unit snapshots the old table first.

**Open question this raises:** if a fresh table still loses to flat bias on max-temp, the
honest move is to turn MOS off in production rather than keep maintaining it. That is a live
model change, so it goes through the shadow harness — and `mos_off` is already running.
Decide it with T4/T5.

## T7 — Workstream A (per-station min-temp MOS) *(unchanged, still parked)*

`PHASE1_STATION_MOS_PLAN.md` T0–T6, entirely unstarted: no `station_mos` in `DEFAULT_SPECS`,
no `skill_path` on `ModelSpec`, no station skill table, `LIVE_EXCLUDED_METRICS` still a global
frozenset rather than `(station, metric)`-keyed. T1 there (IEM international coverage audit)
remains the unfired go/no-go.

Deliberately last. It re-enables a market class that is currently costing nothing, while
T1–T3 recover money already being lost and T4–T6 address a defect affecting every trade. Note
that T5 of that plan — generalizing `LIVE_EXCLUDED_METRICS` to a `(station, metric)` key —
is the natural place to also make the price floor per-station, if it ever needs to be.

---

## Housekeeping — both cleared Sep 16 2026

- ✅ **Gate thresholds restored to 150/21** (`3084fd1`). Inert: the record is 294
  station-resolved over 41 days, so both clear either way. But it surfaced the fact worth
  keeping in view — **with honest thresholds the gate does NOT pass**, failing on quality
  (station PF 1.16 < 1.5; model Brier 0.2277 vs market 0.2229), not sample size. Live runs
  only because `LIVE_GATE_LATCHES` holds the Aug-20 unlock open. Read it as an argument
  against adding capital, not against continuing a ~$3/position test.
- ✅ **`fix/gate-on-executable-ask` MERGED** (`e49d172`, + `cff71bb` for the floor
  interaction). The Aug-27 objection ("sub-floor signals replay at PF 1.90") turned out to
  be contaminated by the cheap-longshot band nobody had isolated yet. Restricted to
  entry ≥ 0.40 with **both arms priced at the ask** — the only honest comparison, since live
  always pays the ask — today's gate returns +$591.67 / PF 1.30 on n=249 against the ask
  gate's **+$594.10 / PF 1.44 on n=176**: the same profit from 29% fewer trades, with the
  73 rejected signals coming in at PF 1.02. Never quote the mid-priced +$654/PF 1.33 figure;
  it is the inflation this change removes, and quoting it is what kept the decision open
  for three weeks.

## Sequencing

**Done 2026-09-15:** T1, T2, T3, T6. T4/T5 are reads, not builds, and are blocked until T2's
re-seeded calibrators have accrued (~2 weeks). T7 stays parked.

T6 landed differently than planned: it refuted its own premise (no tail-shrink term is
warranted) and turned up two concrete bugs instead — the unrunnable MOS rebuild and the
decorative `--validate-only` gate. The remaining model defect it identified (flat ~0.09
overconfidence) routes into T5 rather than a new model knob.
