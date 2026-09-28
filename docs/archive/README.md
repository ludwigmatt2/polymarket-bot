# Archived planning documents

**Nothing in this directory is a work item.** These are historical records. The only active
plan is `docs/PHASE3_STATION_FORECAST_PLAN.md`.

Several of these documents state things that were true when written and are **false now**.
They are kept because the *reasoning* is often still useful — but check any factual claim
against `weather/config.py` and the project memory before acting on it.

| document | status | known-false claims |
|---|---|---|
| `PHASE3` (in `docs/`, **not here**) | **ACTIVE** — the only live plan | — |
| `PHASE2_PRICE_FLOOR_PLAN.md` | Closed out 2026-09-28. All tasks done or answered. Carries the best write-up of the shadow harness and the "report segmented by price band" lesson. | Its band-level edge claims are **in-sample fits**. The sub-$0.40 band it was built on reversed sign out of sample (−0.046 → +0.116). |
| `PHASE1_STATION_MOS_PLAN.md` | Superseded 2026-09-28, **never started**. | "The only station-native forecast is NWS MOS, which is US-only" — **false**, DWD MOSMIX is global. Workstream B's premise that our MOS layer is worth maintaining is also dead (`mos_off` ≈ `prod_mirror` three times). |
| `IEM_INTEGRATION_PLAN.md` | Shipped. Station truth + on-chain resolution are live. | Same "NWS MOS is US-only" claim. |
| `WEATHER_MODEL_UPGRADE.md` | Largely shipped (rounding pre-image, variance inflation, model weighting, 3-model ensemble). | Quotes "64.8% WR and 37% ROI across 355 paper trades" — that record was measured against **Open-Meteo grid truth**, which disagreed with on-chain settlement on ~33% of outcomes. **Those numbers are not real.** |
| `GO_LIVE_RUNBOOK.md` | Executed Aug 20 2026. | Gate thresholds shown as 90/14; restored to the design 150/21 in `3084fd1`. |
| `LIVE_TRADING_BUILD_PLAN.md` | Shipped. | — |
| `WALLET_LIVE_PLAN.md` | Shipped, then substantially reworked by the Aug-31 accounting fixes (`532fd70`, `1d50423`). | Its wallet arithmetic predates the equity-bankroll and double-count fixes. |
| `SECURITY_PLAN.md` | Phases A–F shipped across the `security/*` branches. | — |
| `DEPOSIT_WALLET_EXPERIMENT.md` | Concluded — deposit wallet is what the bot runs on. | — |
| `COMMAND_AUDIT.md` | One-off audit, completed. | — |

## Where current truth lives

1. **`weather/config.py`** — the authority on every live parameter. Trust it over any document here.
2. **Project memory** (`~/.claude/projects/-Users-ludwigmatt-Projects-polymarket-bot/memory/`)
   — current state, findings, and the gotchas that cost real money.
3. **`docs/PHASE3_STATION_FORECAST_PLAN.md`** — what we are actually doing next, and the
   decision criterion for stopping.

## The one lesson worth carrying forward

Three separate engineering decisions across two months were corrupted by the same unsegmented
subpopulation (sub-$0.40 contracts): the Aug-4 running-observation disable, the Aug-27
executable-ask decision, and the Sep-16 `lambda_1_0` evaluation. A fourth — the price floor
itself — was fitted on a band relationship that then reversed out of sample.

**Report every result segmented by price band, and do not make model decisions on windows
shorter than ~250 resolved trades.**
