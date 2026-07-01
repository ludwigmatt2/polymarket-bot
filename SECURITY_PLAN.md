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

## Phase C — Withdrawal hardening (money-loss surface)

**Goal:** make theft-by-withdrawal require more than a leaked bot token.

- [ ] C1. **Per-user withdrawal address allowlist.** First withdrawal to a new address requires an explicit `/allowlist_add <addr>` + confirmation; store allowlisted addresses per uid in `users.json`. Withdrawals to non-allowlisted addresses are rejected. (Wire into the `withdrawconfirm` branch, `telegram_bot.py:1299`.)
- [ ] C2. **Per-user daily withdrawal cap** (e.g. default $X/day, admin-configurable via `/setwithdrawcap`). Sum today's `withdraw` txns from the ledger (`read_wallet`) and block over-cap.
- [ ] C3. **Cooling-off for new allowlist entries** — an address added <24h ago can't be withdrawn to (defeats a smash-and-grab if the bot is briefly compromised).
- [ ] C4. **Large-withdrawal second factor** — above a threshold, require a 6-digit code the bot DMs, or `owner` approval.
- [ ] C5. Keep the existing one-time token flow; add rate-limit (max N withdraw attempts / hour / uid).

**Acceptance:** a withdrawal to a fresh attacker address is impossible without a ≥24h-old allowlist entry; daily loss is bounded by the cap even in full-compromise.

---

## Phase D — Custody / blast-radius reduction

**Goal:** reduce what an attacker gets from full VPS compromise.

- [ ] D1. **Small hot balances.** Keep only working capital in each deposit wallet; sweep excess to a cold address the bot cannot withdraw to. Document target hot-balance policy.
- [ ] D2. **Prefer L2-only where possible.** Investigate holding only L2 CLOB API creds (can trade, cannot move funds off-platform) and *not* the raw L1 key for users who self-fund. Map which flows in `weather/live_trader.py` / `relayer.py` actually need the L1 `pk` (withdrawal/unwrap do; order signing may be doable with L2). Scope a "non-custodial-ish" mode.
- [ ] D3. **Stop leaking generated keys via Telegram.** `generate_wallet()` (`telegram_onboarding.py:128`) currently shows the raw pk in chat. Options: (a) don't display, treat as fully custodial; (b) display once with a hard warning + auto-delete message after N seconds. Decide + implement.
- [ ] D4. **Encrypted, off-site backups** of `data/config/` (store is already Fernet-encrypted). Master key backed up **separately** in a password manager — never in the same location as the store.

**Acceptance:** documented custody model; no raw key ever persisted in Telegram history; a stolen `user_keys.enc.json` is useless without the separately-stored master key.

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
