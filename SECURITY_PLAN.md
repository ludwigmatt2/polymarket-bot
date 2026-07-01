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

## Phase A — `.env` cleanup & secret hygiene

**Goal:** remove per-user/legacy secrets from `.env`, lock down file perms, single source of truth for each value.

- [ ] A1. Verify admin creds are in the encrypted store: `get_user_creds(ADMIN_ID)` returns a dict with `pk`. (One-off check on VPS.)
- [ ] A2. Once A1 passes, **remove** `POLYMARKET_PRIVATE_KEY` and `POLYMARKET_FUNDER_ADDRESS` from VPS `.env`. Confirm bot still boots + reads creds from the store (they're only read by `_seed_admin_creds()` bootstrap, `telegram_bot.py:165`, and a fallback in `weather_bot.py:840` — audit that fallback path and route it through `get_user_creds` instead).
- [ ] A3. Consolidate `ADMIN_ID` → `TELEGRAM_ADMIN_ID`. `weather_bot.py:746,827` read `ADMIN_ID`; `telegram_bot.py:79` reads `TELEGRAM_ADMIN_ID`. Pick one (`TELEGRAM_ADMIN_ID`), update `weather_bot.py`, delete the duplicate.
- [ ] A4. `chmod 600 /opt/polymarket-bot/.env` and `chmod 600 data/config/*.json` (currently `644`). Ensure owned by `bot`. Add a startup assertion in `secrets.py` that warns if `user_keys.enc.json` is group/other-readable.
- [ ] A5. Confirm `.env`, `data/`, `config/` are all git-ignored (`.gitignore` audit) — nothing secret ever committed.
- [ ] A6. Document the final env contract in `README`/`.env.example` (names only): infra keys + `POLYMARKET_SECRETS_KEY` (master) + `BUILDER_*` (operator relayer). Mark which are required vs optional.

**Acceptance:** `.env` contains zero per-user private keys; only infra + master key + builder creds. Bot boots and trades (paper) with keys served exclusively from the encrypted store. All secret files `600`.

---

## Phase B — Graded permissions (RBAC)

**Goal:** replace binary admin/viewer with capability-based roles, managed at runtime (not env).

- [ ] B1. Define roles → capabilities in code (new `weather/permissions.py`, version-controlled):
  - `owner` — all caps incl. `manage_users`, `withdraw_any`, `set_global_config`
  - `admin` — `manage_users`, `view_all`, `trigger_scan`; **not** `withdraw_any`
  - `trader` — `go_live`, `deposit_own`, `withdraw_own`, `set_own_maxbet`
  - `viewer` — read-only, paper only
  - `suspended` — no caps (hard block)
- [ ] B2. Add `has_permission(uid, cap) -> bool` and `require_perm(cap)` decorator. Keep `require_auth`/`is_admin` as thin shims over the new system during migration.
- [ ] B3. Migrate handlers: replace the ~8 `admin_only=True` gates + inline `is_admin()` checks (`telegram_bot.py:660,743,999,1203,1348`) with explicit caps.
- [ ] B4. Role lives in `users.json` (field already exists). Add per-user optional `permissions_override` list for exceptions.
- [ ] B5. New admin commands: `/setrole <uid> <role>`, `/suspend <uid>`, `/unsuspend <uid>`. Guard: only `owner` can create another `admin`/`owner`; nobody can change their own role; `removeuser`/role-change on `ADMIN_ID`/owner is blocked (extend existing `telegram_bot.py:1203` guard).
- [ ] B6. Invites already carry a role (`create_invite(role=…)`, `telegram_onboarding.py:71`) — expose role choice in `/invite` and validate it against allowed set.
- [ ] B7. Command menu per role: `_register_commands` should show only the commands a user's role can run (extend `_ADMIN_COMMANDS`/`_USER_COMMANDS` into a role→commands map).

**Acceptance:** a `viewer` cannot go live or withdraw; a `trader` can act only on their own funds; only `owner` can mint admins; suspended users are hard-blocked at the decorator. Unit tests for `has_permission` cover every role×cap.

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
