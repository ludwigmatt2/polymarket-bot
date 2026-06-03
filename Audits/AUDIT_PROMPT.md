You are a senior staff engineer + security auditor doing a comprehensive, read-only
audit of this project: a REAL-MONEY automated trading bot for Polymarket weather
markets. Real funds move through a live wallet (POLYMARKET_PRIVATE_KEY in .env),
so financial-logic and trade-execution bugs are the highest-severity class of
problem here — a wrong sign or a mis-sized bet loses actual money.

## Operating rules
- READ-ONLY. Do NOT edit, fix, refactor, create any files, or run anything that
  places trades, mutates state files (logs/, config/, *.json, *.csv), or hits live
  trading APIs. Do not write the report to disk — print it to chat only.
- Do NOT print, echo, or exfiltrate secret values. If you find a secret, refer to
  it by file + line + variable name only. Never read .env values aloud.
- You may read code, run static analysis, grep, run the EXISTING tests (read-only),
  and inspect git history. Do not commit.
- Be concrete. Every finding must cite file:line and explain the concrete failure
  mode ("if X, then Y happens, costing/breaking Z"), not vague "consider improving".
- Distinguish what you VERIFIED by reading code from what you're INFERRING.

## Project map (orient yourself first, then verify against reality)
- weather/            core engine: weather_client, probability_model, signal_generator,
                      market_scanner, paper_trader, live_trader (REAL MONEY),
                      position_monitor, price_tracker, backtest, city_bias, models, config
- telegram_bot.py     ~1k-line Telegram UI: multi-user, wallet/positions/deposit/withdraw
- dashboard/          FastAPI server + Three.js globe frontend (static/js/*)
- weather_bot.py      top-level orchestrator
- *_report.py, gap_logger.py, analyze_gaps.py  reporting/analysis scripts
- config/users.json, logs/users/, logs/*.csv, logs/*.json  file-based state
- tests/              pytest suite

## Method (do these passes in order)
PASS 1 — RECON: Build an accurate mental model. Map the data flow end to end:
  weather forecast → probability → market price → signal → sizing → order →
  position → resolution → PnL logging. Identify every place real money or real
  orders are involved, and every place state is persisted. List your assumptions.

PASS 2 — PER-AREA DEEP DIVE: Audit each key area below. For each, apply the listed
  procedures and answer the specific questions. Don't skim — read the actual logic.

PASS 3 — CROSS-CUTTING & SYNTHESIS: Concurrency, secrets, and integration bugs that
  span files. Then produce the final report.

## Key areas + what to check

### A. Financial correctness (HIGHEST PRIORITY)
This is where past bugs lived (git log: "Fix Kelly NO-bet bug", "block equal-YES
signals"). Read the math line by line.
- Kelly sizing: is the formula correct for BOTH yes and no bets? Sign errors,
  fraction caps, bankroll definition, fractional-Kelly multiplier? Can it ever
  size > bankroll, go negative, or divide by zero (edge ~0, price 0/1)?
- Probability model: are forecast→probability conversions sound? Calibration,
  rounding, unit bugs (°C/°F, max vs min temperature — git shows a real
  "temperature_2m_max vs min" metric bug). Threshold/inequality direction for
  "X or higher" vs "X or lower" markets.
- Signal generation & the 9-gate filter: off-by-one, inverted conditions, gates
  that silently pass when data is missing. Does it ever emit a signal on stale or
  null data?
- Expected value & edge: is edge computed against the correct side's price
  (yes price vs 1-no price)? Fees/slippage included?
- Backtest vs live parity: does backtest use the same sizing/signal code paths as
  live, or a divergent copy that could mask bugs? Look-ahead bias, survivorship,
  using resolved outcomes in feature computation.
- Market resolution & PnL: is settlement logic correct? Can a position be
  double-counted, resolved to the wrong side, or PnL computed with wrong cost basis?

### B. Live trading safety (real money path)
- live_trader.py order execution via pmxt: idempotency (could a retry/double-call
  place two orders?), partial fills, order-not-filled handling, price slippage vs
  the price the signal assumed.
- Guardrails: max position size, max daily loss, max open exposure, per-market cap,
  kill switch. Are they enforced BEFORE the order call, and can they be bypassed?
- Failure modes: what happens if the API errors, times out, or returns ambiguous
  status mid-order? Is paper→live gating real (can live fire by accident/misconfig)?
- Funds safety: deposit/withdraw flows in telegram_bot.py — can a user move funds
  they shouldn't, or trigger a withdraw to a wrong/attacker address?

### C. Security & secrets
- Private key (POLYMARKET_PRIVATE_KEY) and PMXT_API_KEY handling: loaded safely?
  Ever logged, included in error messages, sent to the dashboard, or written to
  CSV/JSON state? grep logs/ and dashboard responses for leakage.
- Telegram authz: is TELEGRAM_ADMIN_ID enforced on every privileged command
  (trading, withdraw, config)? Per-user isolation (config/users.json, logs/users/)
  — can user A act on user B's account/wallet? Any command missing an auth check?
- Input validation on Telegram commands and dashboard API: injection, path
  traversal in any file path built from user input, unvalidated numeric input to
  trade size/withdraw.
- Dashboard (FastAPI): unauthenticated endpoints exposing positions/PnL/keys? CORS,
  any endpoint that mutates state via GET?

### D. Concurrency & state integrity
The bot uses file-based state (CSV/JSON) with an async Telegram bot + possibly
launchd/cron + dashboard reading concurrently.
- Race conditions: concurrent read/modify/write of wallet.json, paper_trades.csv,
  positions, last_signals.json. Are writes atomic (temp-file + rename) or can they
  corrupt/truncate on crash or concurrent access?
- CSV schema migrations (git shows past schema-migration fixes): can a stale-schema
  row crash a reader or silently drop fields?
- Async correctness in telegram_bot.py: blocking I/O in async handlers, unawaited
  coroutines, shared mutable state across handlers, job-queue races.

### E. Data pipeline & external APIs
- weather_client.py: multi-source (Open-Meteo / ECMWF) — fallback logic, what
  happens when a source is down, returns partial data, or disagrees. Timezone and
  forecast-horizon correctness (forecasting the RIGHT day for the market).
- market_scanner.py: CLOB/Gamma API usage — pagination, rate limits, retries with
  backoff, handling of malformed/unparseable markets (logs/unparseable_markets.csv).
- Validation: is external data validated before it feeds the probability model and
  a real trade? Null/NaN/out-of-range guards. Stale-data detection (acting on an
  old forecast or old order book).

### F. Reliability & ops
- Error handling: bare excepts that swallow failures, especially around trades and
  state writes. Does a crash mid-trade leave inconsistent state?
- Logging: enough to reconstruct what trades happened and why? Secrets excluded?
- Dockerfile / docker-compose / launchd: restart behavior, does a restart re-fire
  pending trades or duplicate scans?

### G. Testing
- Coverage of the money-path: are Kelly sizing, signal gates, resolution/PnL, and
  live_trader guardrails actually tested, including edge cases (price 0/1, zero
  edge, no/yes symmetry)? Identify untested high-risk functions.
- Test quality: tests that assert nothing, over-mock the thing under test, or would
  pass even if the logic were wrong.

### H. Dependencies & code quality
- requirements.txt: unpinned/loose versions on critical deps (pmxt, scipy, numpy)?
  Known-vuln or abandoned packages?
- Dead code, duplicated logic between backtest/live, copy-paste drift, oversized
  functions in telegram_bot.py and weather_bot.py.

## Severity rubric
- CRITICAL: can lose real money, leak the private key, place wrong/duplicate live
  orders, or let one user touch another's funds.
- HIGH: wrong trading decisions on common inputs, silent data corruption, missing
  auth on a privileged action.
- MEDIUM: edge-case bugs, weak validation, reliability gaps under failure.
- LOW: code quality, maintainability, minor risk.

## Output
Print the full report directly to chat. Do NOT write it to a file. Structure:
1. Executive summary: top 5 risks, one line each, by severity.
2. Data-flow diagram (text) of the money path, with the verified trust boundaries.
3. Findings grouped by area A–H. Each finding:
   - [SEVERITY] Title
   - Location: file:line
   - What's wrong (the concrete failure mode)
   - Evidence (the actual code/behavior you observed)
   - Suggested fix (brief — do NOT implement it)
   - Confidence: Verified | Likely | Needs-runtime-check
4. "Verify-at-runtime" list: things you couldn't confirm statically and how to test
   them safely (paper mode, dry-run).
5. Quick wins vs. deeper refactors.

Start with PASS 1 and show me your data-flow model + assumptions before going deep,
so I can correct any wrong assumptions early.
