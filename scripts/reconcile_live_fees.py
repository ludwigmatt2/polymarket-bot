"""Backfill live_trades.csv `fee_usd` and reconcile net PnL against the wallet.

Why: live_trades.csv `pnl_usd` is GROSS of the Polymarket taker fee (weather/fees.py).
`fee_usd` was only written from 2026-10-15 on, so older resolved rows have it blank and
any "net" figure silently under-counts fees (19 of 229 rows had it). On-chain BUY cost
equals shares*price + 0.05*p*(1-p)*shares exactly, so the blank values are recoverable
from the row itself (fees.row_fee_usd) -- no guessing.

Default is a dry run: reports how many rows would be filled and, with --wallet, checks
every resolved row's recomputed cost against the wallet's actual BUY spend
(data-api.polymarket.com/activity) and flags |diff| > --tol per trade.

Usage (VPS; stop the bot first -- it rewrites this file at resolution):
  venv/bin/python scripts/reconcile_live_fees.py --wallet 0x...          # dry run + check
  venv/bin/python scripts/reconcile_live_fees.py --wallet 0x... --apply  # backup + write
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sys
import urllib.request
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from weather.fees import taker_fee_usd  # noqa: E402
from weather.paths import DATA_DIR  # noqa: E402

_ACTIVITY = "https://data-api.polymarket.com/activity"


def _f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def fee_for(row: dict) -> float:
    """Fee a filled row paid: stake = shares * price. Same formula the live path uses."""
    price = _f(row.get("filled_price"))
    return round(taker_fee_usd(_f(row.get("filled_size")) * price, price), 4)


def backfill(rows: list[dict]) -> int:
    """Fill blank fee_usd on resolved, filled rows in place. Returns rows changed."""
    n = 0
    for r in rows:
        if r.get("resolved_at") and _f(r.get("filled_size")) > 0 and r.get("fee_usd") in (None, ""):
            r["fee_usd"] = fee_for(r)
            n += 1
    return n


def fetch_buy_spend(wallet: str) -> dict[str, float]:
    """conditionId -> total USDC the wallet paid on BUYs (fee included)."""
    spend: dict[str, float] = defaultdict(float)
    offset = 0
    while True:
        req = urllib.request.Request(
            f"{_ACTIVITY}?user={wallet}&limit=500&offset={offset}",
            headers={"User-Agent": "Mozilla/5.0"},  # data-api 403s without a browser UA
        )
        page = json.load(urllib.request.urlopen(req, timeout=30))
        for a in page:
            if a.get("type") == "TRADE" and a.get("side") == "BUY":
                spend[a["conditionId"]] += float(a["usdcSize"])
        if len(page) < 500 or offset >= 5000:
            return spend
        offset += 500


def reconcile(rows: list[dict], spend: dict[str, float], tol: float) -> list[str]:
    """Per resolved row: on-chain spend vs filled_size*filled_price + fee_usd."""
    bad = []
    for r in rows:
        cid = r.get("market_id", "")
        if not (r.get("resolved_at") and _f(r.get("filled_size")) > 0 and cid in spend):
            continue
        expected = _f(r["filled_size"]) * _f(r["filled_price"]) + _f(r.get("fee_usd"))
        diff = spend[cid] - expected
        if abs(diff) > tol:
            bad.append(f"{cid[:10]} {r.get('market_title', '')[:50]}  chain-csv={diff:+.3f}")
    return bad


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--csv", type=Path, default=DATA_DIR / "logs/live_trades.csv")
    ap.add_argument("--wallet", help="funder address; enables the on-chain reconciliation")
    ap.add_argument("--tol", type=float, default=0.01, help="per-trade USD tolerance")
    ap.add_argument("--apply", action="store_true", help="write the backfilled CSV (with .bak)")
    args = ap.parse_args()

    with open(args.csv, newline="") as fh:
        reader = csv.DictReader(fh)
        fields = list(reader.fieldnames or [])
        rows = list(reader)
    if "fee_usd" not in fields:
        fields.append("fee_usd")

    n = backfill(rows)
    resolved = [r for r in rows if r.get("resolved_at") and _f(r.get("filled_size")) > 0]
    gross = sum(_f(r["pnl_usd"]) for r in resolved)
    fees = sum(_f(r["fee_usd"]) for r in resolved)
    print(f"{len(rows)} rows, {len(resolved)} resolved+filled; {n} blank fee_usd filled")
    print(f"gross PnL ${gross:+.2f}  fees ${fees:.2f}  net ${gross - fees:+.2f}")

    if args.wallet:
        bad = reconcile(rows, fetch_buy_spend(args.wallet), args.tol)
        print(f"on-chain check: {len(bad)} trades off by > ${args.tol}")
        for b in bad[:20]:
            print("  ", b)

    if not args.apply:
        print("dry run -- pass --apply to write")
        return
    if not n:
        print("nothing to write")
        return
    shutil.copy2(args.csv, args.csv.with_suffix(".csv.bak"))
    tmp = args.csv.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, args.csv)
    print(f"wrote {args.csv} (backup: {args.csv.with_suffix('.csv.bak').name})")


if __name__ == "__main__":
    main()
