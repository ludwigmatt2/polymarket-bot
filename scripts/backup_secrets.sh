#!/usr/bin/env bash
# Backup the bot's config directory (SECURITY_PLAN Phase D / D4).
#
# What it backs up: data/config/ — the Fernet-encrypted per-user key store
# (user_keys.enc.json) plus users.json / invites.json (roles, allowlists, caps).
# The key store is encrypted at rest, but the backup is still sensitive: guard it.
#
# CRITICAL: the master key (POLYMARKET_SECRETS_KEY) is NOT in this backup, and it
# must NOT be. A backup of the encrypted store is useless without the master key,
# and storing them together defeats encryption-at-rest. Keep the master key in a
# password manager, SEPARATE from these backups.
#
# Usage:
#   scripts/backup_secrets.sh [dest_dir]
#   dest_dir defaults to ./backups (git-ignored). Move/upload the tarball OFF-SITE.
#
# Cron example (daily 03:00, on the VPS):
#   0 3 * * *  cd /opt/polymarket-bot && scripts/backup_secrets.sh /var/backups/pmbot
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

# Resolve the data dir the same way the app does (weather/paths.py).
DATA_DIR="${RAILWAY_VOLUME_MOUNT_PATH:-$PROJECT_ROOT/data}"
if [ -f .env ]; then
  env_data="$(grep -E '^RAILWAY_VOLUME_MOUNT_PATH=' .env | cut -d= -f2- || true)"
  [ -n "${env_data:-}" ] && DATA_DIR="$env_data"
fi

CONFIG_DIR="$DATA_DIR/config"
if [ ! -d "$CONFIG_DIR" ]; then
  echo "ERROR: config dir not found at $CONFIG_DIR" >&2
  exit 1
fi

DEST_DIR="${1:-$PROJECT_ROOT/backups}"
mkdir -p "$DEST_DIR"
chmod 700 "$DEST_DIR"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$DEST_DIR/pmbot-config-$STAMP.tar.gz"

tar -czf "$OUT" -C "$DATA_DIR" config
chmod 600 "$OUT"

echo "✅ Backup written: $OUT"
echo "   Contents: $(tar -tzf "$OUT" | wc -l | tr -d ' ') file(s) from $CONFIG_DIR"
echo
echo "NEXT (manual):"
echo "  1. Move this OFF-SITE (another host / encrypted cloud bucket)."
echo "  2. Keep POLYMARKET_SECRETS_KEY in a password manager, SEPARATE from this file."
echo "     (The store is encrypted; without the master key the backup is useless —"
echo "      which is exactly why they must never live together.)"
