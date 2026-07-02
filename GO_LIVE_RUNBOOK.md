# Go-Live Runbook

_Executable checklist for taking the bot from paper → live with a hardened, encrypted host._
_Covers SECURITY_PLAN **E1** (disk encryption) + **G** (key rotation), plus the go-live gates._
_Commands assume the `pmbot` SSH alias (user `ludo`, passwordless sudo). Do this end-to-end in one sitting._

---

## 0. Decision: how far to go on disk encryption (E1)

The master key (`/etc/polymarket-bot/secrets_key`) and the Fernet store live on the VPS
disk, which is **currently unencrypted**. E1 protects them **at rest** against a Hetzner
snapshot / support access / physical disk theft. Two honest options:

- **Path A — Full-disk LUKS + remote unlock (max).** Rebuild the box encrypted; you SSH
  into the boot initramfs and type a passphrase on every boot. Strongest, but the bot
  does **not** auto-recover from a reboot until you unlock it. Choose this if you'll hold
  meaningful funds.
- **Path B — Accept residual risk (pragmatic).** Skip disk encryption; rely on the
  compensating controls already in place: master key in a root-600 file + systemd
  credential (not `.env`), Fernet-encrypted store, encrypted off-site backups, hardened
  SSH. Reasonable for small balances on a reputable provider.

**Phase G (key rotation) is mandatory either way** — do it whenever a secret has ever
touched plaintext (env, git, chat, this laptop).

If you pick **Path B**, skip §4 and do §1–3, §5(config only), §6.

---

## 1. Pre-flight

- [ ] Paper gates pass: `ssh pmbot 'sudo -u bot systemctl show polymarket-bot >/dev/null'`
      then check `/status` in Telegram shows `resolved ≥ 20` and profit-factor ≥ 1.5.
- [ ] Upstream live-order bug resolved (the py-clob-client deposit-wallet flow) — verify a
      live spike places an order. If still blocked, **do not go live**.
- [ ] You have, in a password manager (NOT on the VPS):
      the current master key, the Telegram bot token, the builder creds, and the admin
      wallet key. If any of these ever leaked, they get rotated in §3.
- [ ] Fresh backup taken (below).

```bash
# Backup the encrypted store + config, pull it to your Mac, keep OFF-SITE.
ssh pmbot 'cd /opt/polymarket-bot && ./scripts/backup_secrets.sh /tmp/pmbot-bk'
scp pmbot:/tmp/pmbot-bk/pmbot-config-*.tar.gz ~/secure-backups/    # off your Mac / to cold storage
ssh pmbot 'shred -u /tmp/pmbot-bk/* && rmdir /tmp/pmbot-bk'
# Record the CURRENT master key so you can decrypt the store on the rebuilt box:
ssh pmbot 'sudo cat /etc/polymarket-bot/secrets_key'   # copy into your password manager
```

---

## 2. Order of operations

```
(Path A) rebuild encrypted box  →  restore data/config  →  install OLD master key
      →  rotate secrets (§3)  →  install NEW master key  →  verify (§6)
(Path B) rotate secrets (§3) in place  →  verify (§6)
```
On a rebuild you restore the store (encrypted with the OLD key), so the OLD key must be
present to decrypt before you rotate to the NEW one.

---

## 3. Phase G — rotate every secret

### G1 — master key (re-encrypt the store)
```bash
ssh pmbot
cd /opt/polymarket-bot
# Rotate: decrypts every blob with the current key, re-encrypts with a fresh one.
sudo -u bot CREDENTIALS_DIRECTORY=/run/credentials/polymarket-bot.service \
     RAILWAY_VOLUME_MOUNT_PATH=/opt/polymarket-bot/data \
     ./venv/bin/python rotate_secrets_key.py
# → prints the NEW master key + a *.pre-rotate-<ts> backup of the store.
```
Install the NEW key and restart:
```bash
printf '%s' '<NEW_KEY_PRINTED_ABOVE>' | sudo tee /etc/polymarket-bot/secrets_key >/dev/null
sudo chmod 600 /etc/polymarket-bot/secrets_key && sudo chown root:root /etc/polymarket-bot/secrets_key
sudo systemctl restart polymarket-bot.service && sleep 15
# verify decryption with the NEW key (see §6), THEN remove the pre-rotate backup:
sudo -u bot rm -f data/config/user_keys.enc.json.pre-rotate-*
```
Put the NEW key in your password manager; delete the OLD one.

### G2 — Telegram bot token
1. In @BotFather → `/revoke` → get a new token (this instantly kills the old one).
2. `sudo nano /opt/polymarket-bot/.env` → set `POLYMARKET_BOT_TOKEN=<new>` → save.
3. `sudo systemctl restart polymarket-bot.service` → confirm "Polymarket Bot online." and `/status` responds.

### G3 — builder / relayer creds
If `BUILDER_API_KEY/SECRET/PASS_PHRASE` were ever exposed, reissue them in the Polymarket
builder dashboard, update `.env`, restart. (If never exposed, optional.)

### G4 — admin wallet key
Only if the admin's L1 key ever sat in plaintext (old `.env`, git, chat):
1. Onboard a fresh wallet via Telegram `/wallet_setup` → "create wallet" (new EOA + deposit wallet), OR generate one and `/setup`.
2. Move funds from the old deposit wallet to the new one (withdraw → allowlisted new address; remember the 24h cooling-off — **allowlist the destination a day ahead**).
3. Retire the old key.

### G5 — confirm nothing leaked
```bash
# On your Mac, from the repo:
git log -p | grep -iE 'PRIVATE_KEY|SECRETS_KEY|BOT_TOKEN|BUILDER_' | grep -v '<redacted>\|example' || echo "clean"
```
Anything that shows up = treat as compromised and rotate it regardless.

---

## 4. Path A — rebuild the VPS with full-disk LUKS + remote unlock

> Hetzner **Cloud** (CX22) has no "encrypt this server" button — you reinstall via a custom
> ISO with LUKS, and add `dropbear-initramfs` so you can unlock remotely at boot. Budget ~1h.
> Everything here is done from the **Hetzner Cloud console** (hands-on), not `pmbot`.

1. **Snapshot first** (rollback safety): Hetzner console → the server → *Snapshots* → create.
2. **Mount an Ubuntu Server ISO**: console → *ISO Images* → attach latest Ubuntu Server LTS → reboot → open the console viewer.
3. **Install with LUKS**: in the installer choose *Custom storage* → *Encrypt the LVM group with LUKS* (guided). Set a strong passphrase (store in your password manager). Finish install, but **before rebooting**, drop to a shell / use the installer's "reconfigure" to add remote unlock:
   ```bash
   # in the installed system (chroot or first boot on console):
   sudo apt-get update && sudo apt-get install -y dropbear-initramfs
   # authorize your unlock key (reuse pmbot_ludo.pub or a dedicated one):
   sudo tee /etc/dropbear/initramfs/authorized_keys < ~/.ssh/pmbot_ludo.pub
   # pin a fixed initramfs SSH port (e.g. 2222) so it won't clash with sshd:
   echo 'DROPBEAR_OPTIONS="-p 2222"' | sudo tee /etc/dropbear/initramfs/dropbear.conf
   sudo update-initramfs -u
   ```
4. **Detach the ISO**, reboot. On boot the server waits for the LUKS passphrase:
   ```bash
   ssh -p 2222 root@204.168.136.136       # you're in the initramfs
   cryptroot-unlock                        # type the LUKS passphrase → boot continues
   ```
5. **Rebuild the app** on the fresh box (see §5).
6. **Reboots now need a manual unlock** (`ssh -p 2222 … cryptroot-unlock`). That's the
   cost of at-rest encryption on a headless host — accept it, or automate later with
   network-bound unlock (clevis + a tang server you run elsewhere).

---

## 5. (Re)deploy the app on the box

```bash
# base packages + user (skip what already exists on Path B)
sudo apt-get install -y python3-venv git fail2ban unattended-upgrades
# non-root user 'ludo' with your key (see SECURITY_PLAN E2) + PermitRootLogin no
# clone + venv
sudo mkdir -p /opt/polymarket-bot && sudo chown ludo:ludo /opt/polymarket-bot
git clone https://github.com/ludwigmatt2/polymarket-bot.git /opt/polymarket-bot
cd /opt/polymarket-bot && python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
# create the 'bot' service user and hand it the data dir
sudo useradd -r -s /usr/sbin/nologin bot 2>/dev/null || true
# restore data/config from your off-site backup:
scp ~/secure-backups/pmbot-config-*.tar.gz pmbot:/tmp/
sudo tar -xzf /tmp/pmbot-config-*.tar.gz -C /opt/polymarket-bot/data && sudo rm /tmp/pmbot-config-*.tar.gz
sudo chown -R bot:bot /opt/polymarket-bot/data && sudo chmod 600 /opt/polymarket-bot/data/config/*.json
# .env (no master key — that's a systemd credential):
sudo -u bot nano /opt/polymarket-bot/.env    # bot token, TELEGRAM_ADMIN_ID, BUILDER_*, intervals, RAILWAY_VOLUME_MOUNT_PATH=/opt/polymarket-bot/data
sudo chmod 600 /opt/polymarket-bot/.env
# master key as a systemd credential (install the key you'll rotate to in §3):
echo '<OLD_master_key_from_backup>' | sudo tee /etc/polymarket-bot/secrets_key >/dev/null
sudo chmod 600 /etc/polymarket-bot/secrets_key && sudo chown root:root /etc/polymarket-bot/secrets_key
# systemd unit + drop-ins (credential + sandboxing) — copy from the old box or recreate:
#   LoadCredential=polymarket_secrets_key:/etc/polymarket-bot/secrets_key
#   NoNewPrivileges=true / PrivateTmp=true / ProtectHome=true / ProtectSystem=full / …
sudo systemctl daemon-reload && sudo systemctl enable --now polymarket-bot.service
```
Then run **§3 (rotate)** so the box ends on fresh secrets, and **§6 (verify)**.

---

## 6. Go-live verification

```bash
ssh pmbot 'sudo systemctl is-active polymarket-bot.service'        # active
ssh pmbot 'sudo journalctl -u polymarket-bot --since "1 min ago" -o cat | grep -i online'
# master key decrypts the store (credential-only):
ssh pmbot 'cd /opt/polymarket-bot && AID=$(grep ^TELEGRAM_ADMIN_ID= .env | cut -d= -f2-); \
  sudo -u bot env CREDENTIALS_DIRECTORY=/run/credentials/polymarket-bot.service \
  RAILWAY_VOLUME_MOUNT_PATH=/opt/polymarket-bot/data TELEGRAM_ADMIN_ID=$AID \
  ./venv/bin/python -c "import os,telegram_bot as t; print(\"selfcheck:\", t._security_self_check() or \"clean\"); \
  from weather.secrets import get_user_creds; print(\"decrypt pk:\", bool((get_user_creds(int(os.environ[chr(84)+\"ELEGRAM_ADMIN_ID\"]))or{}).get(\"pk\")))"'
```
In Telegram (as owner):
- [ ] `/status` responds, `/audit` shows the recent rotation/self-check entries.
- [ ] Region OK: no 403 geoblock (Finland VPS).
- [ ] Allowlist your withdrawal cold address **now** (`/allowlist_add …`) so the 24h clock starts.
- [ ] `/mymode live` → confirm → place a **$1** test order → confirm fill → `/positions`.
- [ ] Withdraw a tiny amount to the allowlisted address (after cooling-off) to prove the full round-trip.

---

## 7. Rollback / recovery

- **Bad rotation:** the store backup `user_keys.enc.json.pre-rotate-<ts>` + the OLD key in
  your password manager restore the previous state. Don't delete either until §6 passes.
- **Locked out of SSH:** Hetzner console → (Path A) unlock via `ssh -p 2222 … cryptroot-unlock`;
  re-enable root login if needed (root's key is still on the box).
- **Bad rebuild (Path A):** restore the pre-rebuild Hetzner snapshot from §4.1.
- **Bot won't start:** `sudo journalctl -u polymarket-bot -n 50 -o cat`; a missing/wrong
  master key shows as the self-check DM "owner has no stored key" + decrypt failures.

---

## Done = plan closed
A ✅ · B ✅ · C ✅ · D ✅ · E (incl. E1 via §4) ✅ · F ✅ · G ✅ (§3). Bot live on an
encrypted, hardened host with rotated secrets, audit logging, and owner alerts.
