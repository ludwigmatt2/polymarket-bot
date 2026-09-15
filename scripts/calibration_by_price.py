#!/usr/bin/env python3
"""Where is the model's confidence wrong — and does the MARKET get it wrong too?

The leaderboard (compare_tracks.py) says whether a track beats the market overall.
This says WHERE it fails, by bucketing resolved signals on the cost of the contract
actually bought and comparing three numbers on identical events:

    model    P(this bet wins) the model claimed  (model_p for YES, 1-model_p for NO)
    market   the same thing per the book         (= entry_price)
    realized what actually happened

Both errors matter. A region where the model is wrong but the market is right is a
model bug worth fixing. A region where BOTH are above realized is a region to stay
out of — no amount of model work earns money there, because the price itself is
biased (the classic favourite-longshot bias).

    venv/bin/python scripts/calibration_by_price.py
    venv/bin/python scripts/calibration_by_price.py --log-dir PATH --since ISO
    venv/bin/python scripts/calibration_by_price.py --by model_p   # tail-ness cut

Sep 2026: this is what showed the sub-$0.40 band is a stay-out region rather than a
calibration bug — model −0.200 against realized, but the market −0.072 as well, so
the price was already too high before the model added its own error. Above $0.40 the
model clears the market by ~+6pp, which is the whole live edge. LIVE_MIN_ENTRY_PRICE
came out of this table.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from weather.config import GATE_ERA_START  # noqa: E402
from weather.paths import DATA_DIR  # noqa: E402

MIN_BUCKET_N = 5   # below this a bucket is noise, not a finding


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _claimed(row: dict) -> float:
    """The model's own P(the bet we placed wins)."""
    p = _num(row["model_p"])
    return p if row.get("direction") == "YES" else 1.0 - p


def _won(row: dict) -> bool:
    yes = row["actual_outcome"] == "1"
    return yes if row.get("direction") == "YES" else not yes


def _load(path: Path, since: str) -> list[dict]:
    """Resolved, non-longshot rows. scan_source="longshot" is a retired Aug-2026
    harvest that is filtered out of the record everywhere else (paper_trader.py,
    weather_bot.py) — including it here would drown every bucket."""
    if not path.exists():
        sys.exit(f"No paper_trades.csv at {path}\n"
                 "  DATA_DIR falls back to the repo root unless RAILWAY_VOLUME_MOUNT_PATH\n"
                 "  is set. On the VPS: sudo -u bot env "
                 "RAILWAY_VOLUME_MOUNT_PATH=/opt/polymarket-bot/data \\\n"
                 "    venv/bin/python scripts/calibration_by_price.py")
    with open(path) as f:
        return [r for r in csv.DictReader(f)
                if r.get("actual_outcome") in ("0", "1")
                and (r.get("scan_source") or "") != "longshot"
                and str(r.get("signal_time", "")) >= since
                and _num(r.get("model_p")) is not None
                and _num(r.get("entry_price")) is not None]


def _table(rows: list[dict], key, label: str, width: float) -> None:
    buckets: dict[float, list] = defaultdict(lambda: [0, 0.0, 0.0, 0])
    for r in rows:
        k = round(key(r) // width * width, 2)
        b = buckets[k]
        b[0] += 1
        b[1] += _claimed(r)
        b[2] += _num(r["entry_price"])
        b[3] += _won(r)

    print(f"{label:<14}{'n':>6}{'model':>8}{'market':>8}{'realized':>10}"
          f"{'mdl err':>9}{'mkt err':>9}  read")
    print("-" * 78)
    for k in sorted(buckets):
        n, sc, se, w = buckets[k]
        if n < MIN_BUCKET_N:
            continue
        model, market, real = sc / n, se / n, w / n
        me, ke = real - model, real - market
        # Who to blame: model-only error is fixable, joint error is a stay-out zone.
        if ke < -0.02 and me < -0.02:
            read = "STAY OUT — price biased too"
        elif me < -0.02:
            read = "model overconfident"
        elif ke > 0.02:
            read = "real edge over market"
        else:
            read = "calibrated"
        print(f"  {k:.2f}-{k + width:.2f}{'':2}{n:>6}{model:>8.3f}{market:>8.3f}"
              f"{real:>10.3f}{me:>+9.3f}{ke:>+9.3f}  {read}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log-dir", default=str(DATA_DIR / "logs"))
    ap.add_argument("--since", default=GATE_ERA_START)
    ap.add_argument("--by", choices=["entry_price", "model_p"], default="entry_price",
                    help="entry_price: cost of the contract bought (default). "
                         "model_p: distance from 0.5, the tail-ness cut.")
    args = ap.parse_args()

    rows = _load(Path(args.log_dir) / "paper_trades.csv", args.since)
    print(f"Resolved paper signals since {args.since} (longshot rows excluded): "
          f"{len(rows)}\n")
    if not rows:
        return

    if args.by == "entry_price":
        _table(rows, lambda r: _num(r["entry_price"]), "entry_price", 0.10)
    else:
        _table(rows, lambda r: abs(_num(r["model_p"]) - 0.5), "|model_p-0.5|", 0.10)

    floor = [r for r in rows if _num(r["entry_price"]) < 0.40]
    rest = [r for r in rows if _num(r["entry_price"]) >= 0.40]
    print()
    for lbl, rs in (("below $0.40", floor), ("at/above $0.40", rest)):
        if not rs:
            continue
        n = len(rs)
        print(f"{lbl:>15}: n={n:<5} model {sum(_claimed(r) for r in rs) / n:.3f}  "
              f"market {sum(_num(r['entry_price']) for r in rs) / n:.3f}  "
              f"realized {sum(_won(r) for r in rs) / n:.3f}")


if __name__ == "__main__":
    main()
