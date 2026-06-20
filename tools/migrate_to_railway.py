#!/usr/bin/env python3
"""
One-time migration: upload local trading data to the Railway volume at /data.

Uses `railway shell -- command` which runs IN the container (not locally),
so it has full access to the mounted volume.

Usage:
    python tools/migrate_to_railway.py

Requires:
    railway CLI installed and linked to this project:
        npm install -g @railway/cli
        railway login
        railway link        # select polymarket-bot project + service
"""
import base64
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).parent.parent

FILES = {
    "logs/paper_trades.csv":      "/data/logs/paper_trades.csv",
    "logs/calibration_log.csv":   "/data/logs/calibration_log.csv",
    "logs/historical_skill.json": "/data/logs/historical_skill.json",
    "logs/city_bias.csv":         "/data/logs/city_bias.csv",
}

for src, dest in FILES.items():
    p = ROOT / src
    if not p.exists():
        print(f"  ✗  {src} — not found, skipping")
        continue

    data_b64 = base64.b64encode(p.read_bytes()).decode()
    dest_path = pathlib.Path(dest)

    # Runs inside the Railway container — has access to the volume at /data
    remote_cmd = (
        f"mkdir -p {dest_path.parent} && "
        f"echo '{data_b64}' | base64 -d > {dest} && "
        f"echo '  ✓  {dest}'"
    )

    print(f"Uploading {src} ({p.stat().st_size:,} bytes) ...", flush=True)
    result = subprocess.run(
        ["railway", "shell", "--", "bash", "-c", remote_cmd],
        capture_output=False,
    )
    if result.returncode != 0:
        print(f"  ❌ Failed (exit {result.returncode})", file=sys.stderr)
        sys.exit(result.returncode)

print("\nMigration complete.")
