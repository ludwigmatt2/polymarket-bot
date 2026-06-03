# Audit Fix Plan — multi-session execution strategy

Companion to [AUDIT_REPORT.md](AUDIT_REPORT.md). Each section below is a self-contained session: a fresh Claude Code conversation can read this file plus the report and pick up the work without prior context.

## How to use this doc

1. Pick a session below.
2. Paste the **Session bootstrap prompt** at the bottom of this file into a new Claude Code conversation, replacing `{N}` with the session number.
3. Claude reads `AUDIT_REPORT.md` (the findings) and this file (the plan), executes only that session's scope, and updates the fix tracker in `AUDIT_REPORT.md` when done.
4. Sessions should be done in order — later ones depend on earlier ones (especially state-integrity work in Session 2).

## Principles driving the sequencing

1. **Quick wins first** — clears the deck and turns the test suite green so regressions show up later.
2. **State integrity before logic changes** — fixing live-trade logic on top of non-atomic writes compounds corruption risk.
3. **B1 (kill switch) and B3 (fill reconciliation) are inseparable** — the kill switch needs live PnL, which needs fill data.
4. **A1 (sizing) ripples** — touches paper PnL, drawdown, and tests; do as one coherent change.
5. **E1 (timezone) needs a data decision** — existing calibration history may be suspect.

## Skip / defer indefinitely
- **H2** (`datetime.utcnow` deprecation) — defended against; only revisit on a Python upgrade.
- **H3** (oversized `telegram_bot.py`) — code quality; do when next adding a feature there.
- **C3** (per-user wallet) — only matters if a second admin user is added.

---

# Session 1 — Quick wins

**Goal:** independent, low-risk fixes that clear the board and get the test suite green.
**Effort:** ~30–60 min.
**Findings:** G2, H1, C2, C4, C5, F3, A5.
**Prerequisites:** none.

### Tasks
1. **G2** — `tests/test_signal_generator.py:214-217`: update `test_edge_is_absolute_difference` expectation from `0.50` to the post-shrinkage value (`0.4025` for the existing inputs), or rewrite the test to mock out the shrinkage so it asserts the raw computation. Pick whichever better expresses intent.
2. **H1** — `requirements.txt`: pin `pmxt==2.35.25`; pin `numpy`, `scipy`, `scikit-learn` to current installed versions (use `pip freeze`).
3. **C2** — `dashboard/server.py:103-108`: change `host="0.0.0.0"` to `host="127.0.0.1"`. Same in `docker-compose.yml` `command:` line. Note in commit: this breaks LAN access by design.
4. **C4** — `telegram_bot.py:766` (`cmd_setup`) and `:787` (`cmd_mymode`): change `@require_auth()` to `@require_auth(admin_only=True)`.
5. **C5** — `telegram_bot.py:698-703` and `:722-727`: after `float(ctx.args[0])`, reject if `amount <= 0` or `not math.isfinite(amount)`; return an error message.
6. **F3** — `telegram_bot.py:599`: `timeout=180` → `timeout=300`.
7. **A5** — `signal_generator.py:206-209`: the comment "Shrinkage never flips direction, so calibrated_p vs yes_price is reliable here" is false in general. Either remove the claim or change the gate to use the post-shrinkage `model_p` (consistent with the final direction decision).

### Definition of done
- `venv/bin/python -m pytest tests/test_paper_trader.py tests/test_signal_generator.py tests/test_probability_model.py` is fully green.
- `requirements.txt` has exact pins on `pmxt`, `numpy`, `scipy`, `scikit-learn`.
- Dashboard binds `127.0.0.1`.
- `/setup` and `/mymode` reject viewer use; deposit/withdraw reject `≤0` / `inf` / `nan`.
- AUDIT_REPORT.md fix-tracker boxes for G2, H1, C2, C4, C5, F3, A5 ticked.

---

# Session 2 — State integrity

**Goal:** atomic writes and concurrency guards before any live-trade logic changes land on top.
**Effort:** ~1–2 h.
**Findings:** D1, D3, D4, F1.
**Prerequisites:** Session 1 complete (green tests).

### Tasks
1. **D1** — introduce a `weather/_io.py` helper `atomic_write_text(path, content)` and `atomic_write_csv(path, headers, rows)` that writes to `path.with_suffix(path.suffix + ".tmp")` then `os.replace`. Migrate:
   - `paper_trader.py:363-367` (`_rewrite_all`)
   - `telegram_bot.py:89-93` (`_save_users`)
   - `telegram_bot.py:153-162` (`append_wallet_transaction` — read-modify-write whole file atomically)
   - `price_tracker.py:43-52` (append-only; consider `with open(..., "a")` is already atomic-ish per-line on POSIX but document the assumption)
2. **D3** — `telegram_bot.py:78-93`: wrap `_load_users`/`_save_users` calls that read-modify-write in a `threading.Lock()` (or `asyncio.Lock` if always called from the event loop). Audit all call sites in `cmd_setup`, `cmd_mymode`, `cmd_adduser`, `cmd_removeuser`, `_seed_admin`.
3. **D4** — `market_scanner.py:131-143`: wrap the `pmxt.Polymarket()` call in `try/finally` so env vars are restored even if the constructor raises. Better: build a minimal `_PublicMarketScanner` that doesn't import pmxt at all (call Gamma directly), avoiding env mutation entirely.
4. **F1** — replace `except Exception: pass` in `probability_model.py:147` and the calibration helpers (`:184-185`, `:193-194`) with `logging` calls that warn. Add a `_calibration_load_error` attribute exposed in `compute_stats` / dashboard so degradation is visible.

### Definition of done
- Crash mid-write (simulate via SIGKILL in a test) leaves `paper_trades.csv` intact.
- Concurrent `cmd_setup` + `cmd_adduser` (test with `asyncio.gather`) preserves both changes.
- Killing pmxt init in scanner restores env vars.
- Calibration load failures are logged.
- AUDIT_REPORT.md boxes for D1, D3, D4, F1 ticked.

---

# Session 3 — Live blocker Part 1: resolve live trades + activate kill switch

**Goal:** make `daily_pnl()` read real data so the −5% halt works, and only log fills that actually happened.
**Effort:** ~2–3 h.
**Findings:** B1, B3, partial G1.
**Prerequisites:** Session 2 (atomic writes).

### Verify-first
Before changing anything, run this read-only check to confirm B1:
```python
# in a throwaway script
import csv
# write a fake row with a large negative pnl_usd dated today
# then call LiveTrader.daily_pnl() and confirm it returns 0.0
```
Expected: `0.0` — proves the column is ignored.

### Tasks
1. **Schema** — `live_trader.py:27-32`: add to `_CSV_HEADERS`:
   - `pnl_usd`, `brier_score`, `actual_outcome`, `resolved_at`
   - `filled_size`, `filled_price`, `order_status` (replaces the static `"submitted"` write)
2. **Fill reconciliation (B3)** — `live_trader.py:105-113`:
   - After `submit_order`, poll order status (use pmxt's `fetch_order` if available; verify the method name first via `inspect`).
   - Only call `self.paper_trader.log_trade(signal)` and write the row if `filled_size > 0`.
   - Record partial fills with the actual filled size, not the requested size.
3. **Live auto-resolve (B1 prerequisite)** — new method on `LiveTrader` mirroring `PaperTrader.auto_resolve`:
   - Iterate unresolved rows in `live_trades.csv`.
   - For each, fetch `actual_outcome` via `weather_client.get_historical_actual` (use the same tz fix from Session 5 if E1 is already done — otherwise note this as a known issue to revisit).
   - Compute `pnl_usd` using `filled_size` and `filled_price`.
   - Write back atomically (using Session 2 helpers).
   - Schedule via a new launchd job or extend the existing `auto-resolve` mode in `weather_bot.py` to handle both logs.
4. **Activate B1** — `live_trader.py:44-56`: now that `pnl_usd` is real, no code change needed; the guard at `:79-84` becomes effective.
5. **Tests (start G1)** — new `tests/test_live_trader.py`:
   - `test_daily_pnl_sums_today_only`
   - `test_kill_switch_halts_on_5pct_loss`
   - `test_no_log_on_unfilled_order`
   - `test_partial_fill_logs_actual_size`
   - Mock pmxt entirely; no network.

### Definition of done
- `daily_pnl()` returns real PnL.
- Unfilled / cancelled orders do not appear in stats.
- Live trades resolve to PnL via the new auto-resolve method.
- `test_live_trader.py` covers the four cases above.
- AUDIT_REPORT.md boxes for B1, B3, partial G1 ticked.

---

# Session 4 — Live blocker Part 2: sizing + idempotency

**Goal:** orders sized correctly in dollars; same market never re-fires.
**Effort:** ~2–3 h.
**Findings:** A1, A3, B2, G3, rest of G1.
**Prerequisites:** Session 3 (live trade resolution).

### Verify-first (A1)
Confirm `amount == contracts` semantically via a dry-run:
```python
poly = pmxt.Polymarket(private_key=..., proxy_address=...)
built = poly.build_order(market_id=..., outcome_id=..., side="buy",
                         type="limit", amount=10, price=0.50)
# DO NOT submit_order. Inspect built — look for a notional field; compare 10*0.50 to it.
```
If the payload shows a $5 cost for `amount=10, price=0.50`, A1 is confirmed.

### Tasks
1. **A1 sizing** — `live_trader.py:58-104`:
   - `kelly_size()` keeps returning USD (semantically clearer); rename to `kelly_size_usd`.
   - In `execute_signal()`, compute `n_contracts = round(size_usd / entry_price, 2)` and pass as `amount`.
   - Rename `MAX_LIVE_TRADE_USD` → keep as USD; assert `size_usd <= MAX_LIVE_TRADE_USD` before the conversion.
2. **A3 drawdown** — `paper_trader.py:254-266`:
   - Compute `real_capital_at_risk = Σ size_usd × entry_price` (since `size_usd` in the paper log is currently a contract count → this is the actual capital).
   - Better: maintain a running equity curve and compute drawdown as `(peak − trough) / peak` on the equity series.
3. **Paper PnL semantics** — decide one of:
   - **(a)** Keep `size_usd` as contract count (matches current behavior + tests). Rename the column/field to `size_contracts`, keep `PAPER_TRADE_SIZE_USD` but compute `size_contracts = PAPER_TRADE_SIZE_USD / entry_price`.
   - **(b)** Treat `size_usd` as the USD stake (correct semantics). Change `resolve_trade`/`auto_resolve` PnL math to `size_usd × (1/entry − 1)` on win, `-size_usd` on lose. Update all G3 tests.
   - Recommendation: **(b)** — semantically correct, makes paper stats directly comparable to live PnL.
4. **B2 idempotency** — `live_trader.py:71-113`:
   - Before `submit_order`, load `live_trades.csv` and skip if there's an unresolved row matching `(market_id, direction)`.
   - Also expose `fetch_positions` via pmxt (verify method name) and skip if any open position in this market.
   - Persist a JSON idempotency key file `logs/live_idempotency.json` mapping `(market_id, direction, date.today())` → `order_id`; check before submit.
5. **Tests (finish G1, fix G3)** — extend `tests/test_live_trader.py`:
   - `test_kelly_size_returns_usd_not_contracts`
   - `test_execute_signal_passes_contracts_to_build_order`
   - `test_execute_signal_skips_existing_position`
   - `test_execute_signal_skips_idempotency_key_match`
   - Update `tests/test_paper_trader.py` G3 cases to assert the new (b) semantics.

### Definition of done
- Live order `amount` equals contracts; capital deployed equals intended Kelly USD.
- Same market never produces two orders in continuous live mode.
- Drawdown denominator reflects real capital.
- Tests pass with the new semantics.
- AUDIT_REPORT.md boxes for A1, A3, B2, G1, G3 ticked.

---

# Session 5 — Resolution timezone + calibration hygiene

**Goal:** resolve trades against the same calendar day they were forecast for; decide what to do with pre-fix calibration data.
**Effort:** ~1–2 h fix + decision time.
**Findings:** E1, G4.
**Prerequisites:** Sessions 3 + 4 (so the fix lands once on stable live + paper paths).

### Verify-first
Pick Tokyo (or Sydney). For one historical date:
```python
loc_local = Location("Tokyo", 35.68, 139.69, timezone="Asia/Tokyo")
loc_utc   = Location("Tokyo", 35.68, 139.69, timezone="UTC")
client.get_historical_actual(loc_local, date(2026,5,15), "temperature_2m_max")
client.get_historical_actual(loc_utc,   date(2026,5,15), "temperature_2m_max")
```
If the two differ, E1 is confirmed for that city.

### Tasks
1. **Store tz on trade row** — `paper_trader.py:27-36`: add `location_tz` to `CSV_HEADERS`. Populate in `_append_trade` from `signal.market.location.timezone`. Same for `live_trades.csv`.
2. **Resolve with stored tz** — `paper_trader.py:184`:
   - Replace `Location(city="", lat=lat, lon=lon, timezone="UTC")` with `Location(... timezone=t.get("location_tz") or "UTC")`.
   - Same change wherever live resolution (Session 3) builds its `Location`.
3. **Calibration decision** — pick one:
   - **(a)** Discard `logs/calibration_log.csv` and re-build from scratch as new trades resolve. Cleanest; loses history.
   - **(b)** Mark all pre-fix rows with a `pre_e1_fix` flag; keep them for now; the model treats them at lower weight or excludes them. Most conservative.
   - **(c)** Backfill: re-resolve every paper trade with the correct tz and overwrite calibration. Requires re-fetching archive data for every resolved trade; cheap (Open-Meteo is free) but slow.
   - Recommendation: **(c)** if the volume is small (<200 resolved trades), otherwise **(a)** — clean break.
4. **Tests (G4)** — `tests/test_paper_trader.py`:
   - `test_auto_resolve_uses_stored_tz`
   - `test_tz_mismatch_yields_different_outcome` (sanity: with the fix, Tokyo resolves to the local-day value, not UTC-day).

### Definition of done
- Stored tz survives a write/read cycle on both logs.
- Auto-resolve uses the stored tz.
- Calibration decision documented in AUDIT_REPORT.md.
- AUDIT_REPORT.md boxes for E1, A2, G4 ticked.

---

# Session 6 — Pre-live safety cluster

**Goal:** close the remaining "right order against a fillable market with a safe key" gaps before the live switch is flipped. After this session the live path is genuinely safe to enable.
**Effort:** ~3–4 h.
**Findings:** B4, E4, C1 (deeper fix).
**Prerequisites:** Sessions 3 + 4 (live blockers closed) + Session 5 (timezone — so calibration isn't lying to gates).

### Verify-first

**B4** — confirm `volumeClob` semantics. In a throwaway script, fetch a couple of weather markets via Gamma and inspect their `volumeClob` vs the live book depth via pmxt:
```python
import pmxt
poly = pmxt.Polymarket()
mkt = poly.fetch_market(id="<condition_id>")
# Inspect mkt.yes / mkt.no and any orderbook attr (look via dir(mkt))
```
Expected: `volumeClob` is cumulative lifetime traded volume, unrelated to current depth. Confirms B4.

**E4** — pull 20 lines from `logs/unparseable_markets.csv` and 20 successfully-parsed market titles from `logs/paper_trades.csv`. Hand-classify direction/threshold for each; check the parser's output against your classification. Note any disagreement — these are the cases tests need to pin.

**C1** — confirm `keyring` is available and macOS Keychain works headless:
```python
import keyring
keyring.set_password("polymarket-bot-test", "uid-0", "test-value")
assert keyring.get_password("polymarket-bot-test", "uid-0") == "test-value"
keyring.delete_password("polymarket-bot-test", "uid-0")
```
If `keyring` fails (e.g., headless Docker), fall back to `cryptography.fernet` with the encryption key in `.env`.

### Tasks

1. **B4 — book-depth liquidity gate**
   - In `market_scanner._parse_market` (`market_scanner.py:280-307`), fetch live book depth alongside the market and attach it to `WeatherMarket` (extend the dataclass with `book_depth_usd: float`).
   - In `signal_generator._quality_gates` gate 5 (`signal_generator.py:187-189`), gate on `book_depth_usd` ≥ `MIN_MARKET_LIQUIDITY_USD × N` (where N is a multiple of intended order size — start with 3× the max Kelly contracts at the entry price, so the gate represents "I can enter and exit without moving the book more than X").
   - Keep `liquidity_usd = volumeClob` as informational (rename the WeatherMarket field if the source disagrees with the name).
   - Add `tests/test_market_scanner.py::test_book_depth_attached` and `tests/test_signal_generator.py::test_gate5_uses_book_depth_not_volume`.

2. **E4 — parser validation against description**
   - In `market_scanner._parse_market` (`market_scanner.py:202-307`), after extracting `threshold`/`direction`/`metric` from the title, re-run a lighter set of regexes against `m.description` (which Gamma already provides at `:113`) and require the extracted values to agree. On disagreement, log to `logs/parser_mismatch.csv` and **drop the market** (do not log to `unparseable` — that's for "couldn't parse"; this is "parsed differently in two places").
   - Add a `tests/test_market_scanner_parser.py` with a parametric test fed by hand-curated `(title, description, expected_threshold, expected_direction, expected_metric)` tuples — include the ambiguous cases from the verify-first step.
   - Backfill ~20 real cases from `logs/unparseable_markets.csv` + `logs/paper_trades.csv` as fixtures.

3. **C1 deeper — encrypted-at-rest key storage**
   - New module `weather/secrets.py` with `get_user_key(uid) -> str | None` and `set_user_key(uid, pk) -> None`.
   - Primary backend: `keyring` (service name `polymarket-bot`, username `uid-<id>`).
   - Fallback backend (when `keyring` unavailable): `cryptography.fernet` with the key in `.env` as `POLYMARKET_SECRETS_KEY` (gitignored already). Encrypted blobs in `config/users.json` under a `private_key_enc` field.
   - Migrate `telegram_bot.py:766-785` (`cmd_setup`) to call `set_user_key(uid, ctx.args[0])` and immediately scrub `ctx.args[0]` from memory.
   - Migrate `telegram_bot.py:585-590` (`run_bot_async`) to call `get_user_key(uid)` instead of reading `private_key` from the JSON.
   - Add a `/setup` reply that warns the user to delete their Telegram message containing the key (best-effort: try `update.message.delete()` first — Telegram allows the bot to delete a user's own private-chat message within 48h if the bot was the recipient).
   - Add `requirements.txt` entries (`keyring>=24.0`, `cryptography>=42.0`); pin exactly.
   - Add `tests/test_secrets.py::test_set_get_roundtrip` and `test_get_returns_none_if_unset`.

### Definition of done
- Live book-depth attaches to `WeatherMarket`; gate 5 uses it.
- Parser disagreement drops the market and logs to `parser_mismatch.csv`; tests pin ~20 real cases.
- No code path reads `private_key` plaintext from `config/users.json`; existing keys (if any) migrated.
- `/setup` instructs the user to delete the message; bot best-effort-deletes its own copy.
- AUDIT_REPORT.md boxes for B4, E4, C1 ticked (C1 status: "fully encrypted").

---

# Session 7 — Model quality / robustness

**Goal:** the signals the live trader acts on actually reflect the model's uncertainty and are validated by a backtest of the path that trades. Not a live blocker, but determines whether going live is worth doing.
**Effort:** ~3–4 h.
**Findings:** E2, E3, A4.
**Prerequisites:** Session 6 (so backtest parity tests don't paper over book-depth/parser fixes).

### Verify-first

**E2** — confirm the false-confidence path. With two of three Open-Meteo models mocked to fail:
```python
# Use a real WeatherClient with two model fetches monkeypatched to raise
# Confirm: forecast.member_arrays has 1 entry, model_means has 1 entry,
# probability_model.compute_probability returns ensemble_spread == 0.0
# Then assert: signal_generator gate 2.7 passes and composite spread_component == 1.0
```

**E3** — `curl -sS "https://gamma-api.polymarket.com/public-search?q=temperature%20London&limit=5" | jq '.events | length'`. Expected: non-zero (Gamma still serves). If zero or error: E3 is no longer hypothetical — port to CLOB immediately.

**A4** — count resolved paper trades and the time range they cover: `wc -l logs/paper_trades.csv` and inspect `min/max(signal_time)`. This sizes the backtest universe.

### Tasks

1. **E2 — require ≥2 models for confidence**
   - In `probability_model.compute_probability` (`probability_model.py:73-78`), when `len(model_breakdown) < 2`, do NOT return `spread = 0.0`. Return `spread = MAX_ENSEMBLE_SPREAD` (force gate 2.7 to fail) OR add a new `RawProbabilityResult.n_models` field and let the gate inspect it.
   - Prefer the explicit-field approach: extend `RawProbabilityResult` with `n_models: int`, and add gate 2.6 ("min model diversity") to `signal_generator._quality_gates` before gate 2.7 — rejects with `gate2.6_single_model:n_models=1`.
   - Adjust gate-8 composite (`signal_generator.py:223-234`): if `n_models < 2`, set `spread_component = 0.0` (not `1.0`) so composite reflects degraded forecast.
   - Tests: `test_compute_probability_single_model_flags_degraded`, `test_gate2.6_rejects_single_model`, `test_gate8_spread_component_zero_when_single_model`.

2. **E3 — Gamma resilience + zero-markets alarm**
   - In `market_scanner._search_keywords` (`market_scanner.py:180-200`), after the loop, if `len(markets) == 0`, log a `WARNING` (using the logger added in Session 2 F1) and write a marker to `logs/scanner_alarm.csv` with timestamp + the search terms tried. The Telegram bot can poll this for push alerts.
   - Add a `--source` option to weather_bot.py that switches between Gamma and CLOB search (CLOB via pmxt's `fetch_markets` — verify the method name in pmxt 2.35.25; introspect first).
   - Default stays Gamma until verify-first shows it's broken.
   - Test: `test_scan_warns_on_zero_markets` (mock urlopen to return empty events).

3. **A4 — backtest through the live SignalGenerator**
   - New class `LiveSignalBacktester` in `weather/backtest.py` (or a new file `weather/live_backtest.py` if `backtest.py` is getting crowded).
   - For each historical resolved paper trade, reconstruct the `WeatherMarket` from stored fields, fetch the *historical* archive forecast (same date, same location, same metric) instead of the live forecast, then call `SignalGenerator.evaluate(market)`.
   - Compare the replayed signal's `direction` and `edge_pp` against what was originally recorded; flag divergences.
   - Score: Brier of replayed `model_p` vs. actual outcome, alongside the original-path Brier; report mean Brier delta.
   - This gives a parity check (does the live signal path produce the same decisions on historical data?) and a quality check (does it score well?).
   - Wire as `weather_bot.py --mode backtest-live`.
   - Test: `test_live_backtester_reproduces_signal_on_fixture` with a single hand-crafted forecast + market.

### Definition of done
- Single-model forecasts cannot pass gates (and composite reflects degradation).
- Zero-markets scans log a visible alarm; CLOB search path exists even if unused.
- `LiveSignalBacktester` runs end-to-end on `logs/paper_trades.csv` and reports parity + Brier metrics.
- AUDIT_REPORT.md boxes for E2, E3, A4 ticked.

---

# Session 8+ — Residual backlog

Independent items; pick whichever the next milestone or actual incident requires.

- **B5** — `live_trader.py:118-125`: drop `""` from the currency match in `fetch_balance`.
- **C3** — per-user wallet under `logs/users/<uid>/wallet.json`. Only needed if a second admin user joins.
- **D2** — single-writer architecture (SQLite WAL or a daemon owning `paper_trades.csv`). Only needed if Session 2's atomic writes prove insufficient under load.
- **H2** — repo-wide `datetime.utcnow()` → `datetime.now(timezone.utc)`. Do alongside the next Python version bump.
- **H3** — split `telegram_bot.py` into handlers/state/formatters; deduplicate paper PnL math between `resolve_trade` and `auto_resolve`. Do when next adding a feature to those files.

---

# Session bootstrap prompt

Paste this into a fresh Claude Code conversation, replacing `{N}` with the session number you want to run:

```
Read AUDIT_REPORT.md and AUDIT_PLAN.md.

Execute Session {N} from AUDIT_PLAN.md. Stay strictly within that session's
scope — do not touch findings from other sessions even if you notice them.

Before any code changes, do the "Verify-first" step if present and report what
you found. Then make the changes, run the tests, and update the fix-tracker
checkboxes in AUDIT_REPORT.md for every finding you resolved.

If you find that a prerequisite session is incomplete, stop and tell me — don't
try to do it yourself in this session.
```
