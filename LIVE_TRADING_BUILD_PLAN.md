# Live trading build-out plan — withdraw (a) + bot wiring (b)

Goal: take the proven deposit-wallet primitives (deploy/fund/wrap/approve/buy/sell/
redeem — all validated standalone) and turn the weather bot into a fully autonomous
live trader, plus a money-out (withdraw) path. Primary account first (deposit wallet
`0xCeE18163EEb650177161a7174b760cf71D45bc8a`, owner EOA `0x9201…`), multi-user later.

Foundational refactor first, then (a), then (b). All relayer ops run from the
Finland VPS; builder creds in `.env` (one set submits batches for any wallet — the
batch is authorized by the owner pk signature, builder creds only auth the relayer
request).

---

## Phase 0 — Consolidate the relayer layer (foundation for a + b)
The spikes duplicate RelayClient setup + batch + nonce. DRY into one module.

- **New `weather/relayer.py`**: `RelayerClient` wrapper built from creds + builder
  creds. Methods (all gasless, auto-read on-chain `nonce()` 0xaffed0e0):
  - `wrap_usdce_to_pusd(wallet, amount)` — approve onramp + wrap
  - `approve_exchanges(wallet)` — pUSD approve + CTF setApprovalForAll to the 3 exchanges
  - `redeem(wallet, calls)` / higher-level `redeem_positions(wallet)`
  - `unwrap_pusd_to_usdce(wallet, amount, to)` — withdraw
  - `_batch(wallet, calls)` — execute_deposit_wallet_batch + auto-nonce + wait
- Port calldata builders (approve/wrap/setApprovalForAll/redeem/unwrap) from spikes.
- **Constants module** `weather/polymarket_v2.py`: pUSD, CTF, onramp, offramp, the 3
  exchanges, adapter, selectors — single source (kill the per-spike copies).
- Validation: re-run wrap/approve via the new module as a no-op (already done) — must
  match spike behaviour.

## Phase 1 — Fix the live order path (the one real code change in execute_signal)
`live_trader.execute_signal` uses the LIMIT path (`create_and_post_order`, size×price)
→ rejects marketable buys with "max 2 decimals". Switch buys to the MARKET-ORDER path.

- In `execute_signal`, replace the order call with
  `create_and_post_market_order(MarketOrderArgsV2(amount=round(size_usd,2), side=BUY,
  order_type=FAK), options=PartialCreateOrderOptions(tick_size, neg_risk), order_type=FAK)`.
- **Slippage cap**: pass `price=` (worst acceptable) on MarketOrderArgsV2 = a band above
  the signal entry_price (e.g. entry_price*(1+MAX_SLIPPAGE), config, default ~3%), so a
  thin book can't fill us far from edge. Skip the trade if best ask > cap.
- Keep the ≥$1 notional guard (market BUY min) and min_order_size logic.
- Read fill from the order RESPONSE (`takingAmount` shares, `makingAmount` USD,
  status MATCHED) — drop the buggy get_order/1e6 path (`filled` scaling).
- Keep all guards: Kelly sizing, daily-loss kill switch, idempotency, balance guard.
- Validation: bot places a real ~$1 live order end-to-end (not a script). Confirm fill,
  log row, idempotency key.

## Phase 2 — Creds + config wiring (make the bot use the deposit wallet)
- Store for the primary user via `secrets.set_user_creds`: `signature_type=3`,
  `funder_address=0xCeE1…`, plus derived clob creds. (Schema already supports these.)
- Persist the deposit wallet address per user (derive via relayer
  `get_expected_deposit_wallet()` at onboarding; store in creds).
- `_make_clob_client` already passes signature_type+funder → no change there.
- Validation: `fetch_balance()` returns the deposit-wallet pUSD ($7.23); a scan that
  produces a signal routes through sig=3 + funder.

## Phase 3 — Claim winnings, wired into the resolve loop (turns paper PnL into real pUSD)
- **`live_trader.claim_winnings()`**: read `redeemable_positions()` (data-api
  redeemable==True), group by conditionId, route CTF vs NegRiskAdapter (logic from
  `spike_redeem.py`), submit ONE relayer batch via `weather/relayer.py`. Log realized
  pUSD per claim. Idempotent (only redeemable; positions vanish after).
- Hook into the existing resolve job: `weather_bot.fan_out_auto_resolve` /
  `telegram_bot._auto_resolve` call `claim_winnings()` after `auto_resolve()`.
- Decouple from weather outcome: claim is driven by on-chain `redeemable` (which lags
  weather resolution), so it self-heals across loops until the market settles on-chain.
- Reconcile: `auto_resolve` keeps computing/logging PnL from weather truth; claim moves
  the funds. Add a `claimed_at`/`redeemed_pnl` column to the live log for audit.
- Validation: can't fully live-test until a position resolves. Dry-run now; first real
  claim on the first resolved live trade. (Optional fast test: buy a market resolving
  within hours, let it settle, claim.)

## Phase A — Withdraw path (money out)
- Verify offramp `unwrap(address,address,uint256)` selector vs bytecode (offramp
  `0x2957922Eb93258b93368531d39fAcCA3B4dC5854`) — first task, like we did for wrap.
- `relayer.unwrap_pusd_to_usdce(wallet, amount, to)`: batch [pUSD.approve(offramp,amt),
  offramp.unwrap(USDC.e, to, amt)]. `to` = external address (straight out) or the EOA.
- Expose as: CLI `weather_bot.py withdraw --amount X --to 0x…` AND a Telegram `/withdraw`
  command (confirmation prompt; never exceed pUSD balance).
- Validation: withdraw a small amount ($1–2) to the user's external address; confirm
  USDC.e arrives.

## Phase 4 — Multi-user generalization (later)
- Onboarding: per-user deploy deposit wallet (relayer) + derive/store funder + sig=3.
- Per-user fund/approve flow (user funds their own wallet; bot runs approve once).
- One global builder-cred set submits all users' batches; each batch signed by that
  user's pk. Verify the relayer accepts multi-wallet under one builder key.
- Per-user claim/withdraw.

---

## Cross-cutting
- **Nonce safety**: serialize relayer batches per wallet (auto-nonce reads on-chain;
  don't fire two batches for one wallet concurrently). A simple per-wallet lock.
- **Builder creds in prod**: already in VPS `.env`; ensure loaded by the bot process
  (systemd EnvironmentFile) — not just the spike scripts.
- **Guards unchanged**: Kelly, daily-loss kill switch, idempotency, balance guard,
  reconcile_positions all stay; verify they hold with the market-order path.
- **Geoblock**: all order/relayer calls already run from the Finland VPS.
- **Rollback**: keep paper mode as the default; live behind the existing
  ready_for_live gate + an explicit live flag.

## Test ladder (do live tests small, on the VPS)
1. Phase 1: one real ~$1 bot-placed order fills.
2. Phase 3: first resolved live trade auto-claims to pUSD.
3. Phase A: $1 withdrawal lands at an external address.
4. Then raise size caps gradually.

## Locked decisions (Jun 30)
- **Claim: AUTO** every resolve loop (`claim_winnings()` after `auto_resolve`, no prompt).
- **Withdraw: TELEGRAM ONLY** — `/withdraw` with a confirmation prompt (no CLI command).
- **Scope: FULL incl. multi-user** (Phase 4 in this build, not deferred).
- Market-order slippage cap default ≈3% (config `MAX_SLIPPAGE`, tunable).
