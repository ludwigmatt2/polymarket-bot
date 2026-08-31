# Command-logic audit (PR5)

Systematic review of every Telegram command for permission gating, mode-awareness,
and honest labelling. Outcome: 2 real fixes + a clean bill for the rest.

## Fixes applied in this PR

### 1. Permission gap — `/scan` and `/resolve` (security)
Both were `@require_auth()` (any authenticated user), yet they trigger a **global**
scan + fan-out that *executes live orders for every live user* — and the equivalent
buttons were already gated on `TRIGGER_SCAN`. Commands now match:
`@require_perm(TRIGGER_SCAN)` → owner/admin only. Traders/viewers use the read-only
`/signals` and `/scanreport` to see scan output instead.

### 2. Mode-awareness — current-view commands showed paper data when live
`/positions`, `/trades`, `/losses`, `/why` (and the `why:` buttons) read the paper
log unconditionally, so a **live** account saw its paper mirror, not real trades.
Added `_active_trades_csv_path(uid)` (live log when live, paper otherwise) and routed
all six current-view readers through it. Paper-stat readers (`read_stats`,
`wallet_stats`, `_count_trades`) deliberately keep reading paper. `/positions` now
also excludes errored live rows (order never placed ≠ open position); `/trades`
badges 🟢 Live / 🟡 Paper.

## Reviewed and correct (no change)

| Command | Gate | Notes |
|---|---|---|
| `/help` `/status` `/paperstats` `/wallet` `/positions` `/signals` `/trades` `/why` `/scanreport` `/losses` | `require_auth` | read-only; `/status`+`/wallet` mode-scoped (PR3), views mode-scoped (this PR) |
| `/deposit` | `DEPOSIT_OWN` | mode-aware (PR2) |
| `/withdraw` `/allowlist` `/allowlist_add` `/allowlist_remove` | `WITHDRAW_OWN` | withdrawal-policy gated (Phase C) |
| `/exportkey` | `require_auth` | own key only; audited + 60s auto-delete (PR4) |
| `/setup` | `USE_LEGACY_SETUP` | overwrite-guarded (PR1) |
| `/mymode` | `require_auth` | paper is free; live requires `GO_LIVE` + wallet + gate + one-time confirm token |
| `/setwithdrawcap` | `MANAGE_USERS` | |
| `/setmaxbet` | `SET_MAXBET` | |
| `/users` | `VIEW_USERS` | |
| `/audit` `/adduser` `/removeuser` `/suspend` `/unsuspend` | `MANAGE_USERS` | |
| `/setrole` | `MANAGE_ROLES` | |
| `/invite` | `MANAGE_USERS` (in handler) | |

## Deferred / minor (non-blocking)
- `/losses` and `/why` are schema-compatible across paper/live logs, but their
  narrative text is paper-tuned; wording could be refined for live trades later.
- `/help` lists all commands regardless of role; could be filtered per-capability.
