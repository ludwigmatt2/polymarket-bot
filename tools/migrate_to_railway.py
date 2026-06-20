#!/usr/bin/env python3
"""
One-time migration: upload local trading data to Railway volume at /data.

Usage:
    python tools/migrate_to_railway.py

Requires:
    railway CLI installed and linked to this project:
        npm install -g @railway/cli
        railway login
        railway link        # select polymarket-bot project
"""
import base64
import io
import pathlib
import subprocess
import sys
import tarfile

ROOT = pathlib.Path(__file__).parent.parent

# Local src → Railway dest
FILES = {
    "logs/paper_trades.csv":      "/data/logs/paper_trades.csv",
    "logs/calibration_log.csv":   "/data/logs/calibration_log.csv",
    "logs/historical_skill.json": "/data/logs/historical_skill.json",
    "logs/city_bias.csv":         "/data/logs/city_bias.csv",
}

# ── Pack ──────────────────────────────────────────────────────────────────────
print("Packing files...")
buf = io.BytesIO()
packed = []
with tarfile.open(fileobj=buf, mode="w:gz") as tar:
    for src, dest in FILES.items():
        p = ROOT / src
        if not p.exists():
            print(f"  ✗  {src} — not found, skipping")
            continue
        # Strip leading / so arcname is a relative path; we restore it on extraction
        tar.add(p, arcname=dest.lstrip("/"))
        print(f"  ✓  {src}  ({p.stat().st_size:,} bytes)")
        packed.append(dest)

if not packed:
    print("\nNothing to migrate — exiting.")
    sys.exit(0)

payload_b64 = base64.b64encode(buf.getvalue()).decode()
print(f"\n  Packed {len(packed)} file(s): {len(buf.getvalue()):,} bytes → {len(payload_b64):,} chars (base64)")

# ── Remote extraction script (runs inside Railway container) ─────────────────
remote_script = r"""
import base64, io, sys, tarfile, pathlib
raw  = base64.b64decode(sys.stdin.buffer.read())
with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
    for member in tar.getmembers():
        dest = pathlib.Path("/") / member.name   # restore leading /
        dest.parent.mkdir(parents=True, exist_ok=True)
        if member.isfile():
            fobj = tar.extractfile(member)
            dest.write_bytes(fobj.read())
            print(f"  ✓  {dest}  ({dest.stat().st_size:,} bytes)")
print("Migration complete.")
"""

# ── Upload ────────────────────────────────────────────────────────────────────
print("\nUploading to Railway (railway run) ...")
result = subprocess.run(
    ["railway", "run", "--", "python3", "-c", remote_script],
    input=payload_b64.encode(),
    # stdout / stderr go straight to terminal
)
sys.exit(result.returncode)
