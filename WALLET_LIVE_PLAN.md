# Wallet, Deposit & Live-Stat Redesign — Implementation Plan

Locked decisions:
- **A1 — live `/deposit` is address-only** (show deposit-wallet address + on-chain balance; the ROI basis comes from *detected* on-chain deposits, never a typed number).
- **B1 — read-only Polymarket viewing now** (public profile / data-API by address, no key exposure) + a guarded `/exportkey` so the user *can* retrieve the key to view/trade via the Polymarket UI later. Full "connect-to-UI" walkthrough deferred.
- **C1 — live-only stats when live** (no paper reference line in `/status`/`/wallet`; paper record stays viewable via `/paperstats`).

## Verified architecture (grounding)
- Deposit wallet (`signature_type=3`, `funder_address` = `0xCeE1…`) is the only path that trades via the bot.
- Live execution logs a **shadow paper trade** (`live_trader.py` `execute_signal` → `paper_trader.log_trade`), so `paper_trades.csv` + the global calibration keep growing in live mode. Going live *adds* real execution; it does not stop the paper record.
- Calibration is global (root scan resolves only); per-user resolves never feed it.
- `live_trades.csv` is per-user and empty at go-live → the live track record is inherently a clean slate; no deletion needed.

## Ship as 5 sequenced PRs (branch → tests → PR → merge → VPS deploy)

### PR 1 — Wallet safety & re-entry  (tasks #6, #5)
- `allow_reentry=True` on the onboarding ConversationHandler (fixes `/wallet_setup` returning nothing mid-conversation).
- Overwrite guard: `ob_create` refuses to regenerate over an existing key — shows current wallet + explicit `[🔴 Replace]` / `[⬅️ Keep current]` confirm.
- Overwrite warnings on the connect path and `/setup` (require explicit `replace`).
- Fix `cmd_setup` help text ("config/users.json" → encrypted `user_keys.enc.json`).

### PR 2 — Mode-aware `/deposit`  (task #7)
- Paper mode: current ledger, clearly labeled simulated.
- Live mode: address-only funding surface + live on-chain balance (relayer usdce+pUSD) + Check-balance button.
- New `weather/live_ledger.py` (real per-user deposit ledger, separate from paper `wallet.json`); record detected on-chain deposit deltas (idempotent).
- Refactor onboarding `_balance_reply` into a shared balance helper.

### PR 3 — Mode-scoped stats / clean live slate  (task #9)
- Stamp `went_live_at` at go-live.
- `/status` + `/wallet` mode-scoped: live → live_trades.csv + real-deposit basis (return% = live PnL ÷ net real deposits); paper → today's behavior.
- `/paperstats` keeps the paper/calibration record viewable.
- Go-live gate (global paper record) unchanged.

### PR 4 — wallet_setup rework + Polymarket viewing  (task #10, B1)
- Deposit wallet is the canonical identity+funding address.
- Rework connect path: pasted EOA key → derive *its* deposit wallet; drop obsolete proxy sig=1 storage + `PROXY_PASTE`.
- Read-only Polymarket profile/data-API viewing info.
- Guarded, audited, auto-deleting `/exportkey` (owner-only).

### PR 5 — Full command audit  (task #8)
- Systematic pass over all commands: permission gating, mode-awareness, honest paper/real labeling, dead paths. Findings table + fixes.

## Sequencing
```
PR1 (safety) → PR2 (deposit + live_ledger) → PR3 (mode-scoped stats)
                                   ↘ PR4 (setup rework + viewing) → PR5 (audit)
```
PR1 first (fund-loss risk + unblocks testing). PR2→PR3 ordered (PR3 consumes PR2's live ledger). PR4/PR5 independent after.

## Risks & mitigations
- Overwrite guard must be high-friction (explicit red confirm) — never accidental.
- Deposit detection: record deltas only on explicit balance-check, idempotent, reconciled to on-chain truth; never trust typed numbers.
- Every stat view badges 🟡 PAPER / 🟢 LIVE so the scope is always clear.
