# Security & Multi-User Hardening Plan

_Status: proposed (2026-07-01). Bot runs paper 24/7 on Hetzner Helsinki VPS. No real user funds live yet — this plan is to be executed **before** go-live. Key rotation (Phase G) happens at go-live._

## Current state (baseline)

- **Per-user secrets already isolated.** Private keys are Fernet-encrypted per uid in `data/config/user_keys.enc.json` via `weather/secrets.py`. `data/config/users.json` holds roles/metadata (no keys). This is correct — `.env` is not the per-user secret store.
- **Permission model is binary.** `require_auth(admin_only=…)` + `is_admin()`/`is_authorized()` → only `admin` vs `viewer`.
- **Withdrawal** (`on_button` → `withdrawconfirm`) already uses a one-time confirmation token, but has **no address allowlist and no daily cap**.
- **Gaps found:** admin's raw `POLYMARKET_PRIVATE_KEY`/`FUNDER_ADDRESS` still sit in plaintext `.env`; `config/*.json` are `644` (world-readable); `ADMIN_ID` duplicates `TELEGRAM_ADMIN_ID`; onboarding `generate_wallet()` prints the raw private key into Telegram chat history; custodial model = one VPS compromise drains all wallets.

## Guiding principles

1. Minimize secrets in plaintext env; one master key, protected hardest.
2. Least privilege — every action gated by an explicit capability.
3. Shrink blast radius — assume the VPS *will* eventually be compromised.
4. The withdrawal path is the only irreversible money-loss surface — treat it as critical.
5. Everything auditable; every privileged action logged append-only.

---

## Phase A — `.env` cleanup & secret hygiene ✅ COMPLETE (2026-07-01)

**Goal:** remove per-user/legacy secrets from `.env`, lock down file perms, single source of truth for each value.

- [x] A1. Verified admin creds in the encrypted store: `get_user_creds(admin)` returns pk + funder + sig=3 + L2 CLOB creds.
- [x] A2. Removed `POLYMARKET_PRIVATE_KEY` + `POLYMARKET_FUNDER_ADDRESS` from VPS `.env`; bot boots + serves creds from the store. (The `weather_bot.py` env fallback only fires when the store is empty — verified it does not fire; left as a harmless one-time migration path for a brand-new admin.)
- [x] A3. Consolidated `ADMIN_ID` → `TELEGRAM_ADMIN_ID` in `weather_bot.py` (×2), `validate_live_order.py`, `enable_deposit_wallet.py` (backward-compatible fallback); deployed via PR #2; removed `ADMIN_ID` from VPS `.env`.
- [x] A4. `chmod 600` on `.env` + `data/config/*.json` (were `644`), owned by `bot`. _(Startup readability assertion in `secrets.py` folded into Phase F3.)_
- [x] A5. `.env` + `config/` git-ignored, no secret files tracked; fixed stale `.gitignore` comment; added `.env.bak*`.
- [x] A6. Rewrote `.env.example` to the real contract: documents `POLYMARKET_SECRETS_KEY` (master) + `BUILDER_*` (operator relayer); removed raw per-user key vars.

**Acceptance:** ✅ `.env` contains zero per-user private keys; only infra + master key + builder creds. Bot boots and paper-trades with keys served exclusively from the encrypted store. All secret files `600`. Service verified `active`, online, 0 poller conflicts post-change.

---

## Phase B — Graded permissions (RBAC) ✅ COMPLETE (2026-07-01)

**Goal:** replace binary admin/viewer with capability-based roles, managed at runtime (not env).

- [x] B1. `weather/permissions.py` (pure, no telegram imports) defines roles `owner/admin/trader/viewer/suspended` → capability frozensets. `owner`=all incl. `withdraw_any`; `admin`=all except `withdraw_any`; `trader`=view/go_live/deposit_own/withdraw_own; `viewer`=view; `suspended`=∅.
- [x] B2. `has_permission(uid, cap)` + `require_perm(cap)` decorator in telegram_bot.py; `is_admin`/`is_authorized` now derive from the role model (owner counts as admin). Primary admin auto-seeded/migrated to `owner`.
- [x] B3. Migrated all gates: `@require_perm(...)` on setmaxbet/setup/users/adduser/removeuser/deposit/withdraw; inline `has_permission` on scan cooldown, main_kb buttons, on_button scan/resolve/users, and the live/withdraw confirm branches; `go_live` check added to `/mymode live`.
- [x] B4. Per-user `permissions_override` list in users.json honored by `has_permission`.
- [x] B5. `/setrole`, `/suspend`, `/unsuspend` with guards: owner-only to mint/alter admin/owner, no self-changes, owner/ADMIN_ID protected; suspend preserves prior role for restore.
- [x] B6. `/invite` and `/adduser` validate the requested role via `can_assign_role` (admins can't mint admin/owner); accept `trader`.
- [x] B7. Per-role command menu: `_register_commands` sets the admin menu per owner/admin chat, base menu as the default.

**Acceptance:** ✅ viewer can't go live/withdraw; trader acts only on own funds; only owner mints admins; suspended hard-blocked at the decorator. `tests/test_permissions.py` — 17 tests, full role×cap matrix + guards + override + wiring — all pass.

---

## Phase C — Withdrawal hardening (money-loss surface) ✅ COMPLETE (2026-07-01)

**Goal:** make theft-by-withdrawal require more than a leaked bot token.

Pure decision logic in `weather/withdrawal_policy.py` (`evaluate_withdrawal`), config
in `weather/config.py`, storage + rate-limit + commands in `telegram_bot.py`.

- [x] C1. **Per-user withdrawal allowlist** in `users.json` (`withdraw_allowlist`). `/allowlist_add`, `/allowlist_remove`, `/allowlist` (view). Non-allowlisted destinations are rejected in both `cmd_withdraw` and the `withdrawconfirm` re-check.
- [x] C2. **Per-user daily cap** (`WITHDRAW_DAILY_CAP_USD` default, per-user override via admin `/setwithdrawcap`); `withdrawn_today()` sums today's ledger withdrawals; over-cap blocked (and re-checked at confirm).
- [x] C3. **24h cooling-off** (`WITHDRAW_COOLING_OFF_HOURS`) — a freshly allowlisted address is `cooling` (unusable) until the window elapses; `/allowlist` shows "ready in Nh".
- [x] C4. **Large-withdrawal code** (`WITHDRAW_LARGE_USD`) — single withdrawals ≥ threshold require a re-entered 6-digit code (`/withdraw <amt> <addr> <code>`); the owner is alerted on any non-owner large withdrawal. _(Note: the code shares the Telegram channel, so it's friction + audit, not true out-of-band 2FA — real 2FA/owner-approval-gate deferred.)_
- [x] C5. Existing one-time confirm token kept; added per-uid hourly attempt rate limit (`WITHDRAW_MAX_ATTEMPTS_PER_HR`).

**Acceptance:** ✅ a withdrawal to a fresh attacker address is impossible without a ≥24h-old allowlist entry; daily loss is bounded by the cap even in full compromise. Tests: `tests/test_withdrawal_policy.py` (25) + `tests/test_withdrawal_bot.py` (6).

---

## Phase D — Custody / blast-radius reduction ✅ COMPLETE (2026-07-01)

**Goal:** reduce what an attacker gets from full VPS compromise.

**Key finding (D2):** automated order placement on Polymarket CLOB requires the raw
L1 private key — every order is EIP-712 **signed** with it (`_make_clob_client(pk=…)`
in `weather/live_trader.py`; L2 CLOB creds only *authenticate* API calls, they can't
sign orders). So a pure "L2-only, hold no L1 key" non-custodial mode is **infeasible**
for an autonomous bot: to trade for the user, the bot must hold the signing key.
Corollary that reshapes the threat model: **Phase C's allowlist/cooling-off protect
against a compromised bot _session_, not a compromised _key store_.** An attacker with
the raw keys can move funds on-chain directly, bypassing the bot. Against key-store
compromise the only real bounds are (a) small hot balances [D1] and (b) protecting the
master key [Phase E]. This makes D1 + E the substantive blast-radius controls.

- [x] D1. **Hot-balance policy (documented).** Deposit wallets should hold only working
  capital; sweep excess to a **cold address the bot's automation won't touch**. The
  sweep mechanism already exists — a Phase C withdrawal to an allowlisted cold address
  (allowlist + 24h cooling-off apply). Automated periodic sweeping is deferred until
  live with non-trivial balances (currently paper). Policy: keep ≤ a few days of
  deployable capital hot per user; everything else cold.
- [x] D2. **Investigated → custodial is inherent** (see finding above). No non-custodial
  mode; effort redirected to D1 + Phase E. Documented rather than built.
- [x] D3. **Already implemented.** `generate_wallet()` never prints the key (the create
  path does `del pk`); key reveal is opt-in (`ob_reveal`) with a hard warning and a
  60s auto-delete (`_delete_message_job`); pasted keys on the connect path are deleted
  from chat (`telegram_onboarding.py`). No raw key persists in chat history.
- [x] D4. **Backup mechanism shipped** — `scripts/backup_secrets.sh` writes a
  timestamped `600` tarball of `data/config/` (Fernet-encrypted store + users/invites).
  Off-site upload is a documented manual/cron step; the master key stays in a password
  manager, **separate** from backups (the script prints this rule). `backups/` is
  git-ignored.

**Acceptance:** ✅ custody model documented (custodial is inherent; blast-radius bounded
by hot balance + master-key protection); no raw key persisted in Telegram history;
`data/config/` backup exists and a stolen store is useless without the separately-held
master key.

---

## Phase E — VPS / infra hardening

- [ ] E1. Confirm/enable full-disk encryption (LUKS) on the VPS. If not possible post-install, at minimum encrypt the `data/` volume.
- [ ] E2. SSH: key-only auth (`PasswordAuthentication no`), non-root login, `fail2ban`.
- [ ] E3. `ufw` firewall — outbound only what's needed; no inbound except SSH.
- [ ] E4. `unattended-upgrades` for security patches; pin Python deps + enable Dependabot/`pip-audit` in the repo.
- [ ] E5. Run the bot as non-root `bot` user (already the case per `polymarket-bot.service`) — verify no writable paths outside `data/`.
- [ ] E6. Move `POLYMARKET_SECRETS_KEY` out of `.env` into **systemd `LoadCredential=`** (or `sops`/`age`/a KMS), so reading `.env` alone doesn't yield the master key. Update `secrets.py` to read from the credential path if present, falling back to env.

**Acceptance:** master key not recoverable from `.env` alone; disk encrypted; SSH hardened; automated patching on.

---

## Phase F — Audit logging & monitoring

- [ ] F1. Append-only audit log (`data/logs/audit.jsonl`): every live order, withdrawal, role change, cred access, mode flip → `{ts, uid, action, details}`.
- [ ] F2. Admin alerts on sensitive events: any withdrawal, any new admin/owner, any suspended-user access attempt → Telegram DM to owner.
- [ ] F3. Startup self-check that reports (to owner) if any config file is world-readable, master key is env-only, or a user has a role but no creds.
- [ ] F4. Reuse the existing dedup-poller detection (see `dup_poller_gotcha` memory) — alert instead of silently conflicting.

**Acceptance:** every privileged action is reconstructable from the audit log; owner is notified in real time of money-movement and permission changes.

---

## Phase G — Key rotation runbook (execute AT go-live)

_You said you'll roll the important keys at go-live. This is the checklist._

- [ ] G1. **`POLYMARKET_SECRETS_KEY`** — write a `rotate_secrets_key.py`: load store with old key, re-encrypt every blob with a freshly generated Fernet key, atomic-swap the file, update the key in its store (systemd cred/KMS). Keep old key until verified.
- [ ] G2. **`POLYMARKET_BOT_TOKEN`** — regenerate via BotFather, update `.env`/cred store, restart service. (Invalidates the old token immediately — good.)
- [ ] G3. **`BUILDER_API_KEY/SECRET/PASS_PHRASE`** — rotate via Polymarket if they were ever exposed.
- [ ] G4. **Admin L1 key** — if the admin wallet key was ever in plaintext env/history, generate a new EOA, move funds, update the encrypted store, retire the old key.
- [ ] G5. Post-rotation: grep git history + logs to confirm no secret was ever committed; if any was, treat it as compromised and rotate regardless.

**Acceptance:** every secret in use at go-live is one that has never touched plaintext git/history/chat; old values invalidated.

---

## Recommended sequencing

1. **Now (pre-go-live, cheap + high value):** Phase A (env hygiene), Phase E1–E5 (VPS hardening), F1/F3 (audit log + self-check).
2. **Before real funds:** Phase B (RBAC), Phase C (withdrawal hardening) — these are the multi-user gates you actually asked for.
3. **At go-live:** Phase G (rotate everything), E6 (master key off env), F2 (alerts).
4. **Ongoing / stretch:** Phase D (custody model, non-custodial investigation) — biggest structural win, largest effort.

## Rough effort

| Phase | Effort | Risk if skipped |
|---|---|---|
| A env hygiene | S | plaintext key on disk |
| B RBAC | M | can't safely onboard non-trusted users |
| C withdrawal | M | unbounded theft on compromise |
| D custody | L | full-drain blast radius |
| E infra | S–M | VPS-level compromise |
| F audit | S | no forensics / no alerting |
| G rotation | S | go-live with tainted keys |
