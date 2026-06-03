# Security & Financial Audit — Polymarket Weather Trading Bot

**Date:** 2026-05-28
**Scope:** Read-only audit of the real-money trading path (weather engine, live trader, Telegram bot, dashboard, ops).
**Method:** Full read of the money path; existing tests run; `pmxt 2.35.25` API introspected to resolve order semantics.
**Status at audit time:** Paper mode only. No live trades placed (`logs/live_trades.csv` absent). No private key stored (`config/users.json` key = `null`). launchd runs paper + auto-resolve only; live fires only from manual `python weather_bot.py --mode live`.

**Companion plan:** [AUDIT_PLAN.md](AUDIT_PLAN.md) — multi-session execution strategy. Use the bootstrap prompt at the bottom of that file to run one session per Claude Code conversation.

> Secrets are referenced by location only — no key material is reproduced here.

---

## Fix tracker

Severity: 🔴 CRITICAL · 🟠 HIGH · 🟡 MEDIUM · ⚪ LOW

| ID | Sev | Title | Location | Status |
|----|-----|-------|----------|--------|
| C1 | 🔴 | Private key plaintext + `config/` not gitignored + via Telegram | `telegram_bot.py:766-785`, `.gitignore` | ✅ gitignore fixed 2026-05-28; fully encrypted 2026-06-03: weather/secrets.py (keyring primary, fernet fallback); cmd_setup uses set_user_key + scrubs plaintext + warns/deletes message; run_bot_async uses get_user_key; 5 secrets tests |
| B1 | 🔴 | Daily-loss kill switch is inert | `live_trader.py:44-56,79-84,27-32` | ✅ pnl_usd written on fill; auto_resolve populates it; kill switch now live 2026-06-02 |
| B2 | 🔴 | No idempotency → duplicate live orders | `live_trader.py:71-113` | ✅ idempotency JSON + CSV + positions check before submit 2026-06-02 |
| A1 | 🟠 | Orders sized in contracts, not USD | `live_trader.py:58-104` | ✅ kelly_size_usd; n_contracts = size_usd/entry_price passed to build_order 2026-06-02 |
| E1/A2 | 🟠 | Resolution timezone mismatch corrupts go-live gate | `weather_client.py:291` vs `paper_trader.py:184` | ✅ location_tz stored on every trade row; auto_resolve uses stored tz in both paper + live paths; fallback "UTC" for pre-fix rows 2026-06-02 |
| B3 | 🟠 | No fill reconciliation; paper mirror records phantom fills | `live_trader.py:105-113` | ✅ fetch_order polls fill; only logs + mirrors on filled_size>0; records actual filled_size/price 2026-06-02 |
| D1 | 🟠 | Non-atomic state writes; `_rewrite_all` truncates in place | `paper_trader.py:363-367` | ✅ atomic_write_csv/_text via weather/_io.py 2026-06-02 |
| G1 | 🟠 | Live money path has zero test coverage | `live_trader.py` (no tests) | ✅ 14 tests in test_live_trader.py covering kill switch, fill reconciliation, sizing, idempotency 2026-06-02 |
| A3 | 🟡 | Max-drawdown gate denominator dimensionally incoherent | `paper_trader.py:254-266` | ✅ equity-curve drawdown; denominator = total USD staked (option-b semantics) 2026-06-02 |
| A4 | 🟡 | Backtest is a divergent code path from live signal generation | `backtest.py` vs `probability_model.py` | ✅ LiveSignalBacktester in weather/live_backtest.py; replay_trades builds archive-based synthetic ensemble, runs full signal pipeline; --mode backtest-live; 5 tests 2026-06-03 |
| B4 | 🟡 | Liquidity gate uses traded volume, not book depth | `market_scanner.py:109`, `signal_generator.py:187` | ✅ book_depth_usd attached via pmxt fetch_order_book; gate 5 uses depth when sidecar available, falls back to volume; 3 tests 2026-06-03 |
| C2 | 🟡 | Dashboard unauthenticated on `0.0.0.0`; can control bot | `dashboard/server.py:103-108`, `api.py:45-89` | ✅ bound to 127.0.0.1 2026-06-02 |
| C3 | 🟡 | Wallet ledger is global, not per-user | `telegram_bot.py:42,148-209` | ☐ |
| D2 | 🟡 | Multiple concurrent writers to the same CSV | telegram/dashboard/launchd | ☐ |
| D3 | 🟡 | `_users_cache` mutated without locking | `telegram_bot.py:78-93` | ✅ threading.Lock guards all read-modify-write sites 2026-06-02 |
| D4 | 🟡 | Scanner pops env vars without try/finally | `market_scanner.py:131-143` | ✅ try/finally restores env vars 2026-06-02 |
| E2 | 🟡 | Single-model forecast yields false-high confidence | `probability_model.py:73-78`, `signal_generator.py:183,223` | ✅ n_models field on RawProbabilityResult; gate 2.6 rejects n_models<2; gate 8 spread_component=0 when n_models<2; 5 tests 2026-06-03 |
| E3 | 🟡 | Gamma API dependency / silent empty scans | `market_scanner.py:123,180-200` | ✅ zero-markets alarm to scanner_alarm.csv + WARNING log; CLOB fallback via --source clob; Gamma confirmed alive 2026-06-03; 2 tests 2026-06-03 |
| E4 | 🟡 | Title parsing can mis-extract threshold/direction | `market_scanner.py:202-307` | ✅ description cross-check in _parse_market; direction/threshold conflict drops market + logs to parser_mismatch.csv; 5 parser tests 2026-06-03 |
| F1 | 🟡 | Broad `except: pass` hides calibration/state failures | `probability_model.py:134-148` | ✅ logs warnings + calibration_load_error attribute 2026-06-02 |
| G2 | 🟡 | Test suite is red on `main` (1 failing) | `tests/test_signal_generator.py:214-217` | ✅ fixed 2026-06-02 |
| G3 | 🟡 | PnL tests lock in the USD/contract ambiguity | `tests/test_paper_trader.py:108-123` | ✅ tests + paper_trader updated to option-b USD semantics (win=stake*(1/price-1), loss=-stake) 2026-06-02 |
| H1 | 🟡 | Money-path deps unpinned (`pmxt>=2.35.0`, numeric stack) | `requirements.txt` | ✅ fixed 2026-06-02 |
| A5 | ⚪ | "Shrinkage never flips direction" is false; gate 9.6 uses pre-shrink prob | `signal_generator.py:115-130,205-209` | ✅ comment corrected 2026-06-02 |
| C4 | ⚪ | `/setup` and `/mymode live` available to viewers | `telegram_bot.py:765-807` | ✅ fixed 2026-06-02 |
| C5 | ⚪ | deposit/withdraw accept negative / inf / nan | `telegram_bot.py:698-703,722-727` | ✅ fixed 2026-06-02 |
| B5 | ⚪ | `fetch_balance` matches empty-string currency | `live_trader.py:118-125` | ☐ |
| F3 | ⚪ | Telegram scan timeout (180s) shorter than scan duration | `telegram_bot.py:599` | ✅ fixed 2026-06-02 |
| H2 | ⚪ | `datetime.utcnow()` deprecation throughout | repo-wide | ☐ |
| H3 | ⚪ | Duplication / oversized modules | `paper_trader.py`, `telegram_bot.py` | ☐ partial: scanner DI seam (poly/weather_client injectable) + __new__ fixture removed 2026-06-03; paper_trader/telegram_bot duplication still open |

---

## Money-path data flow & trust boundaries

```
EXTERNAL (untrusted) ── Open-Meteo (forecast/archive/geocode) ── Polymarket Gamma API
        │ members                                    │ market dict (title, price, VOLUME)
        ▼                                            ▼
 weather_client.get_ensemble_forecast()      market_scanner._parse_market()
   tz = location.tz   ◄═ TZ-A                  regex title→threshold/dir/metric; liquidity = VOLUME ◄═ LIQ
        └────────────────────┬───────────────────────┘
                             ▼  WeatherMarket
              signal_generator.evaluate()
              prob_model → 11 gates → shrinkage → direction → Signal
                 ┌───────────┴────────────┐ gate_passed
        PAPER    ▼                         ▼   LIVE
 paper_trader.log_trade()         live_trader.execute_signal()
  dedupe (mkt,dir); size=25×f      is_unlocked? · daily_pnl<-5%? ◄═ DEAD (B1)
  append paper_trades.csv          size=kelly_size() USD
        │                          build_order(amount=size ◄═ CONTRACTS (A1), price)
        │                          submit_order() ◄═ no idempotency (B2)
        ▼                          append live_trades.csv (no pnl) · MIRROR→paper log (B3)
 auto_resolve() launchd 14:00      [no live resolution → no live pnl ever]
  actual tz="UTC" ◄═ TZ-B (≠TZ-A → wrong-day, E1)
  _rewrite_all() ◄═ truncate, non-atomic (D1)
        ▼
 compute_stats() → go-live gate (PF≥1.5, BSS≥0, ≥20 trades, DD≤20%) → UNLOCKS LIVE
```

**Trust boundaries (verified):**
- `.env` `POLYMARKET_PRIVATE_KEY` → CLI/launchd live path; gitignored.
- `config/users.json` per-user `private_key` (Telegram `/setup`) → now gitignored (fixed); still plaintext at rest; currently `null`.
- Telegram: `require_auth()` on all handlers; live never fires from Telegram (`/scan` hardcodes `--mode paper`, `telegram_bot.py:741`).
- Dashboard: `0.0.0.0:8765`, no auth, paper-only.
- Live orders fire only from manual `python weather_bot.py --mode live`.

---

## Findings

### A. Financial correctness

#### 🟠 A1 — Live orders are sized in contracts, but the code computes a USD figure
- **Location:** `live_trader.py:58-69` (`kelly_size`), `:86-104` (`execute_signal`)
- **What's wrong:** `kelly_size()` returns `bankroll_usd * KELLY_FRACTION * full_kelly` — a dollar stake — and it is passed straight into `build_order(amount=round(size,2), ...)`. pmxt documents `amount` as "Number of contracts." Actual USD deployed = `amount × price = size × entry_price`, not `size`.
- **Evidence:** `pmxt 2.35.25` `build_order` docstring: *"amount: Number of contracts; price: Limit price (0.0-1.0)."* `--bankroll 500` quarter-Kelly yielding `size=$25` orders 25 contracts (≈ `$25 × entry_price` at risk); `MAX_LIVE_TRADE_USD=25` actually caps contracts.
- **Fix:** order `amount = size_usd / entry_price`; rename the cap to reflect USD-notional intent.
- **Confidence:** Verified (pmxt introspected).

#### 🟠 A2 — Resolution corrupts the go-live gate via timezone mismatch
- See **E1** for the mechanism. Impact: wrong win/Brier/PnL → distorts the stats that unlock live money.
- **Confidence:** Verified mechanism; per-market magnitude Needs-runtime-check.

#### 🟡 A3 — Max-drawdown gate denominator is dimensionally incoherent
- **Location:** `paper_trader.py:254-266`
- **What's wrong:** numerator (`peak − cumulative`) is per-contract PnL in dollars; denominator `hypothetical_capital = sum(size_usd)` sums contract counts. The ratio is dollars/contracts. `max_dd` is driven near-zero, so the `MAX_PAPER_DRAWDOWN_PCT=20%` gate effectively never trips (matches the prior "2.1% vs 25.4%" note).
- **Fix:** track real capital at risk (`Σ size×entry_price`) and compute drawdown against a running equity curve.
- **Confidence:** Verified.

#### 🟡 A4 — Backtest is a divergent code path from live signal generation
- **Location:** `backtest.py` (climatology / Gaussian-residual / blend) vs `probability_model.compute_probability` (ensemble-fraction + KDE + calibration + gates + shrinkage)
- **What's wrong:** the backtest validates a different model than what trades; live gates, calibration, shrinkage, and sizing are never exercised by it. The only validation of the live path is paper trading, itself corrupted by A2/D1.
- **Fix:** add a backtest mode that replays historical markets through `SignalGenerator`/`ProbabilityModel`, or a parity test pinning a fixed forecast → signal.
- **Confidence:** Verified.

#### ⚪ A5 — "Shrinkage never flips direction" is false; gate 9.6 uses pre-shrink prob
- **Location:** `signal_generator.py:115-130`, gate 9.6 `:205-209`
- **What's wrong:** shrinkage pulls `model_p` toward 0.5; if `market_p` lies between 0.5 and the original `model_p`, the shrunk value can cross `market_p` and flip `direction`. Gate 9.6 blocks equal-YES on `prob.calibrated_p` (pre-shrink) while final `direction` uses post-shrink `model_p`. Low impact (edge near zero at the flip).
- **Confidence:** Verified; low impact.

### B. Live trading safety

#### 🔴 B1 — Daily-loss kill switch is dead code
- **Location:** `live_trader.py:44-56` (`daily_pnl`), `:79-84` (guard), `:27-32` (`_CSV_HEADERS`)
- **What's wrong:** `daily_pnl()` sums `row["pnl_usd"]` from `live_trades.csv`, but `_CSV_HEADERS` has no `pnl_usd` column, the writer uses `extrasaction="ignore"`, and nothing ever resolves a live trade or writes its PnL. `daily_pnl()` always returns `0.0`; the −5% halt never triggers.
- **Evidence:** `pnl_usd` appears only in the reader (`:51,53`), never a writer. The only remaining per-trade guard is the (mis-sized, A1) per-order cap.
- **Fix:** resolve live trades (mirror `auto_resolve` into `live_trades.csv`, writing `pnl_usd`) before relying on `daily_pnl`; until then treat live as having no loss circuit-breaker.
- **Confidence:** Verified.

#### 🔴 B2 — No idempotency / open-position check → duplicate orders
- **Location:** `live_trader.py:71-113`; driver loop `weather_bot.py:563-573,72-85`
- **What's wrong:** `execute_signal()` never checks for an existing position/order in the market. In continuous `--mode live --interval N`, a market that passes gates re-fires an order every interval until it leaves the entry window. The paper mirror (`:111`) dedupes, but `execute_signal` ignores that result and submits the live order first regardless.
- **Fix:** before submitting, check `live_trades.csv` / `fetch_positions` for an existing position in `market_id`+side and skip; persist an idempotency key per (market, direction, day).
- **Confidence:** Verified.

#### 🟠 B3 — No fill reconciliation; paper mirror records unconditional "fills"
- **Location:** `live_trader.py:105-113`
- **What's wrong:** a limit order is placed at the signal's assumed price, logged `status="submitted"`, then `paper_trader.log_trade(signal)` is called unconditionally. If the book moved and the order doesn't fill (or partially fills), stats still count a full position and `live_trades.csv` shows "submitted" forever. Recorded and real holdings diverge.
- **Fix:** poll order status after submit; only log/mirror on confirmed fill; record filled size/price; handle partial fills.
- **Confidence:** Verified.

#### 🟡 B4 — Liquidity gate uses traded volume, not order-book depth
- **Location:** `market_scanner.py:109` (`liquidity = volumeClob or volume`), gate 5 `signal_generator.py:187-189`
- **What's wrong:** `MIN_MARKET_LIQUIDITY_USD` is checked against cumulative volume, but the docstring claims "enough USD in the book." High lifetime volume with a thin current book passes, then a live order eats slippage or doesn't fill.
- **Fix:** fetch live bid/ask depth and gate on executable size at the intended price.
- **Confidence:** Verified.

#### ⚪ B5 — `fetch_balance` matches empty-string currency
- **Location:** `live_trader.py:118-125`
- **What's wrong:** the currency filter includes `""`, so a blank-currency balance entry is treated as USDC. Could read the wrong balance on an unexpected payload.
- **Confidence:** Verified; low likelihood.

### C. Security & secrets

#### 🔴 C1 — Private key plaintext + (was) not gitignored + via Telegram
- **Location:** `telegram_bot.py:766-785` (`/setup`), `.gitignore`
- **What's wrong:** `/setup <private_key>` stores the key verbatim in `config/users.json`. `config/` was not in `.gitignore` (one `git add .` from being committed) — **fixed 2026-05-28**. The key is also sent as a normal Telegram message, persisting in chat history and on Telegram's servers. Any `viewer` can call `/setup` (`require_auth()` without `admin_only`). Currently `private_key=null`, so unexploited.
- **Fix (remaining):** store keys via OS keychain / encrypted-at-rest, not plaintext JSON; instruct users to delete the `/setup` message and prefer a file-based secret; gate `/setup` to `admin_only`.
- **Confidence:** Verified.

#### 🟡 C2 — Dashboard is unauthenticated on `0.0.0.0` and can control the bot
- **Location:** `dashboard/server.py:103-108` (`host="0.0.0.0"`), `dashboard/api.py:45-89`
- **What's wrong:** no auth, no CORS restriction. Anyone on the LAN can `GET /api/trades` (full history/PnL) and `POST /api/scan|start|stop|interval`. No secrets exposed and it can't place live orders, but it's unauthenticated control + data disclosure.
- **Fix:** bind `127.0.0.1` by default, add an auth token, restrict CORS.
- **Confidence:** Verified.

#### 🟡 C3 — Wallet ledger is global, not per-user
- **Location:** `telegram_bot.py:42` (single `WALLET_FILE`), `:148-209`
- **What's wrong:** `read_wallet`/`append_wallet_transaction`/`wallet_stats` ignore `uid` — one shared `logs/wallet.json`. Deposits/withdrawals by any admin appear in every user's `/wallet` and return %, mixed with that user's per-user trades. Breaks per-user isolation. (Bookkeeping only — no real funds move.)
- **Fix:** store transactions under `logs/users/<uid>/wallet.json`.
- **Confidence:** Verified.

#### ⚪ C4 — `/setup` and `/mymode live` available to viewers
- **Location:** `telegram_bot.py:765-807`
- **What's wrong:** both are `require_auth()` not `admin_only`. Harmless today (Telegram `/scan` always runs paper) but a future wiring of `get_user_mode()` into `run_bot_async` would let a viewer trigger live trading.
- **Confidence:** Verified.

#### ⚪ C5 — deposit/withdraw accept negative / inf / nan
- **Location:** `telegram_bot.py:698-703,722-727`
- **What's wrong:** `float(ctx.args[0])` accepts `-500`, `inf`, `nan`; corrupts the wallet ledger / return %. Bookkeeping only.
- **Confidence:** Verified.

> **Positive:** no secret leakage in `logs/`. Sweep for `private_key` tokens and long `0x` hex found only Polymarket condition IDs in CSVs, not keys.

### D. Concurrency & state integrity

#### 🟠 D1 — All state writes are non-atomic; `_rewrite_all` truncates in place
- **Location:** `paper_trader.py:363-367`; also `telegram_bot.py:89-93` (`_save_users`), `:153-162` (wallet), `price_tracker.py:43-52`
- **What's wrong:** `_rewrite_all` opens `paper_trades.csv` in `"w"` (truncate) then writes rows. A crash or concurrent reader mid-write yields a truncated/empty history — silently resetting go-live gate stats. No temp-file+`os.replace` anywhere.
- **Fix:** write to `*.tmp` then `os.replace()` (atomic on POSIX); take a file lock around read-modify-write.
- **Confidence:** Verified.

#### 🟡 D2 — Multiple concurrent writers to the same CSV
- **Location:** `telegram_bot.py:577-603` (subprocesses), `dashboard/scanner_bridge.py:90-95`, launchd jobs
- **What's wrong:** Telegram scan, Telegram auto-resolve, dashboard loop, and launchd jobs can all read-modify-write `logs/paper_trades.csv` concurrently. With D1, lost updates / corruption are likely under overlap.
- **Confidence:** Verified by design (no locking).

#### 🟡 D3 — `_users_cache` global mutated without locking
- **Location:** `telegram_bot.py:78-93`
- **What's wrong:** concurrent handlers (`/adduser` + `/setup`) each load → mutate → save; the later writer clobbers the earlier change (could drop a stored key or a new user).
- **Confidence:** Verified.

#### 🟡 D4 — Scanner pops env vars without try/finally
- **Location:** `market_scanner.py:131-143`
- **What's wrong:** `os.environ.pop("POLYMARKET_PRIVATE_KEY")` etc. then `pmxt.Polymarket()`; restore is only on the happy path. If the constructor raises, keys are gone from the process env (fail-safe for trading, but breaks a same-process live trader). Mutating global env is also an async race hazard.
- **Fix:** wrap in `try/finally`, or pass explicit public-mode args instead of mutating `os.environ`.
- **Confidence:** Verified.

### E. Data pipeline & external APIs

#### 🟠 E1 — Forecast/resolution timezone mismatch
- **Location:** `weather_client.py:285-308` (members fetched with `timezone=location.timezone`), `:223-238` (`get_historical_actual`), `paper_trader.py:184` (`Location(... timezone="UTC")`)
- **What's wrong:** signals are computed on the location's local-day temperature; outcomes are resolved on the UTC-day temperature. For cities far from UTC the daily max differs. Wrong outcome → wrong PnL, Brier, and calibration observations fed back into the model (`paper_trader.py:207-208`).
- **Fix:** resolve with the same timezone used at signal time (store the market's tz on the trade row and reuse it).
- **Confidence:** Verified mechanism.

#### 🟡 E2 — Single-model forecast yields false-high confidence
- **Location:** `probability_model.py:73-78`; gate 2.7 `signal_generator.py:183-185`; gate 8 `:223-234`; per-model fetch `weather_client.py:192-198`
- **What's wrong:** `ensemble_spread` is set to `0.0` when `<2` models return data. If two of three models fail (each `except: continue`), spread = 0 → gate 2.7 passes and the gate-8 `spread_component = 1.0` (maximum confidence) on effectively a single model. A degraded fetch masquerades as agreement.
- **Fix:** require ≥2 distinct models for a non-zero-spread signal; treat `<2 models` as high uncertainty.
- **Confidence:** Verified.

#### 🟡 E3 — Gamma API dependency / silent empty scans
- **Location:** `market_scanner.py:123,180-200`
- **What's wrong:** scanning hits `gamma-api.polymarket.com/public-search` via `urllib`, wrapped in `except Exception: print(warning)`; on failure `scan()` can return `[]` with only a printed warning. Memory notes Gamma was deprecated (May 1 → CLOB); degradation silently stops market discovery.
- **Fix:** confirm Gamma `public-search` still works; add a "0 markets" alarm; fall back to the CLOB search path.
- **Confidence:** Needs-runtime-check.

#### 🟡 E4 — Title parsing can mis-extract threshold/direction → wrong-side real trade
- **Location:** `market_scanner.py:202-307`
- **What's wrong:** thresholds/direction come from regex over free-text titles with no cross-check against the resolution description. `_DEGREE_PATTERN` takes the first number; `>` is strict while "X or higher" implies `≥`; "equal" uses a ±0.5°C band. A misparse produces a confidently wrong `model_p` and a real order. Unparseable markets are logged; mis-parsed ones are silently traded.
- **Fix:** validate parsed threshold/direction against the description; require agreement before tradeable; add parser unit tests over real titles.
- **Confidence:** Verified (risk); frequency Needs-runtime-check.

### F. Reliability & ops

#### 🟡 F1 — Broad `except: pass` hides state/calibration failures
- **Location:** `probability_model.py:134-148` (calibration load), `:184-185,193-194`; `weather_client.py:192-198,237-238,263-265`
- **What's wrong:** a corrupted `calibration_log.csv` is swallowed and silently disables calibration, so the model reverts to raw probabilities with no operator signal.
- **Fix:** log exceptions; surface "calibration disabled" in stats/dashboard.
- **Confidence:** Verified.

#### ⚪ F2 — Restart behavior
- launchd runs paper + auto-resolve only and `log_trade` dedupes by `(market_id, direction)`, so a re-fired scan is mostly idempotent. Danger is operator-driven: adding `--mode live` to a scheduler re-triggers B2.
- **Confidence:** Verified.

#### ⚪ F3 — Telegram scan timeout shorter than scan duration
- **Location:** `telegram_bot.py:599` (`timeout=180`) vs UI text "~2–3 min"
- **What's wrong:** a slow scan hits the 180s timeout → "Scan failed" even on success.
- **Confidence:** Verified.

### G. Testing

#### 🟠 G1 — The live money path has zero test coverage
- **Location:** no test references `live_trader`
- **What's wrong:** `kelly_size`, `execute_signal`, the (dead) kill switch, idempotency, fill handling, and `daily_pnl` are entirely untested. A1 and B1 would both have been caught by a single unit test.
- **Confidence:** Verified.

#### 🟡 G2 — Test suite is red on `main`
- **Location:** `tests/test_signal_generator.py:214-217`
- **What's wrong:** `test_edge_is_absolute_difference` expects `edge_pp == 0.50` but shrinkage (`signal_generator.py:115-119`) makes it `0.4025`. Running the 3 core suites: **1 failed, 43 passed**. Tests aren't gating commits.
- **Fix:** update the expectation (assert post-shrinkage behavior); wire tests into a pre-merge check.
- **Confidence:** Verified (ran it).

#### 🟡 G3 — PnL tests lock in the USD/contract ambiguity
- **Location:** `tests/test_paper_trader.py:108-123`
- **What's wrong:** tests assert `25×(1−0.30)=17.50` / `−25×0.30=−7.50` (contract interpretation) while config/labels say USD. They'd pass even though dollar semantics (A1) are wrong → false assurance.
- **Confidence:** Verified.

#### ⚪ G4 — Resolution / `_evaluate_outcome` / timezone untested
- The E1 bug has no test that would catch it.
- **Fixed 2026-06-02:** 3 tests added to `tests/test_paper_trader.py::TestTimezoneResolution`:
  `test_auto_resolve_uses_stored_tz`, `test_auto_resolve_fallback_to_utc_when_no_tz_stored`,
  `test_tz_mismatch_yields_different_outcome`.

**Calibration decision (2026-06-02):** 295 resolved trades exist (>200 threshold) — option **(a) clean break** recommended: discard `logs/calibration_log.csv` to remove observations resolved under the wrong (UTC) timezone, then rebuild from scratch as new trades resolve.
Run: `mv logs/calibration_log.csv logs/calibration_log.pre_e1_fix.csv` to archive before deletion.

### H. Dependencies & code quality

#### 🟡 H1 — Money-path deps unpinned
- **Location:** `requirements.txt`
- **What's wrong:** `pmxt>=2.35.0` (and `numpy/scipy/scikit-learn` lower-bound only). A minor `pmxt` release that changes `build_order`'s `amount` semantics or signing would silently alter live behavior. `python-telegram-bot==21.9` is correctly pinned.
- **Fix:** pin `pmxt==2.35.25` and the numeric stack exactly; bump deliberately with a re-test.
- **Confidence:** Verified.

#### ⚪ H2 — `datetime.utcnow()` deprecation throughout
- Py 3.12 warns; naive/aware datetimes are patched defensively in spots (`signal_generator.py:166-169`, `paper_trader.py:163-164`) but the mix is fragile. `probability_model.py:159`, `models.py:51`.

#### ⚪ H3 — Duplication / size
- PnL logic duplicated (`paper_trader.py:104-110` vs `:196-204`); user-list rendering duplicated (`telegram_bot.py:810-815` vs `925-930`); `telegram_bot.py` is a 1k-line module mixing data access, formatting, and handlers.

---

## Verify-at-runtime checklist (safe)

1. **A1 sizing** — call `kelly_size(signal)` + `build_order(...)` against pmxt testnet/dry-run (do NOT `submit_order`); compare `amount × price` to the intended stake.
2. **B1 kill switch** — craft a `logs/live_trades.csv` with a large negative `pnl_usd` row dated today; confirm `daily_pnl()` returns `0.0`.
3. **B2 idempotency** — run live (sandbox/mocked pmxt) over two cycles; assert two `submit_order` calls for one market.
4. **E1 timezone** — for Tokyo/Sydney, compare `get_ensemble_forecast` (local tz) vs `get_historical_actual(timezone="UTC")` daily-max for the same date.
5. **E2 single-model** — mock two of three model fetches to fail; confirm `ensemble_spread==0.0` and the signal passes gate 2.7 with `spread_component=1.0`.
6. **E3 Gamma** — `curl` the `public-search` endpoint for one term; confirm non-empty results.

## Suggested fix order

**Before enabling live (blockers):** B1, B2, A1, E1.
**Quick wins:** C1 gitignore (done) → narrow to `config/users.json` if shared config is later needed; bind dashboard to `127.0.0.1` (C2); pin `pmxt` (H1); fix the red test (G2); `admin_only` on `/setup` (C4); atomic `_rewrite_all` (D1).
**Deeper refactors:** correct sizing semantics end-to-end (A1/A3); add live resolution + idempotency + fill reconciliation with tests (B1/B2/B3/G1); unify resolution timezone (E1); file locking / single state-writer (D1/D2/D3); backtest through the live `SignalGenerator` (A4).
