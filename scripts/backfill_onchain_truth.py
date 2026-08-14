"""Cross-check paper-trade outcomes against Polymarket's ACTUAL on-chain
settlement, independent of any weather-truth proxy.

Why: paper_trades.csv (mainline AND longshot) is labeled by our own weather
pipeline (Wunderground/IEM station reading, or an Open-Meteo grid fallback) via
paper_trader.auto_resolve. That's a proxy for what Polymarket really paid out —
resolution_source.py already found ~33% grid-truth disagreement with on-chain in
an earlier check, and the longshot track's outlier wins are disproportionately
grid-labeled (Aug 14 finding). This script reads the market's real settlement
straight from the ConditionalTokens contract's payoutNumerators
(weather.relayer.chain_outcome) and reports how the honest PF/WR/PnL compares to
what's currently recorded, split by track (mainline vs longshot).

Usage (VPS, full era):
  RAILWAY_VOLUME_MOUNT_PATH=/opt/polymarket-bot/data venv/bin/python \
      scripts/backfill_onchain_truth.py            # dry-run report
  ... --apply                                       # write onchain-relabeled copy back
"""

from __future__ import annotations

import argparse
import csv
import shutil
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from weather.config import GATE_ERA_START  # noqa: E402
from weather.paper_trader import _brier, _trade_pnl  # noqa: E402
from weather.paths import DATA_DIR  # noqa: E402
from weather.relayer import chain_outcome  # noqa: E402

MAX_WORKERS = 8


def fetch_chain_truth(condition_ids: set[str]) -> dict[str, bool | None]:
    """One chain_outcome() lookup per unique conditionId, run concurrently."""
    out: dict[str, bool | None] = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(chain_outcome, cid): cid for cid in condition_ids}
        done = 0
        for fut in as_completed(futs):
            cid = futs[fut]
            try:
                out[cid] = fut.result()
            except Exception:
                out[cid] = None
            done += 1
            if done % 100 == 0:
                print(f"  ... {done}/{len(condition_ids)} conditions checked", flush=True)
    return out


def _track_of(row: dict) -> str:
    return "longshot" if (row.get("scan_source") or "") == "longshot" else "mainline"


def _bucket() -> dict:
    return {"n": 0, "wins": 0, "gw": 0.0, "gl": 0.0, "pnl": 0.0}


def _add(b: dict, pnl: float) -> None:
    b["n"] += 1
    b["pnl"] += pnl
    if pnl > 0:
        b["wins"] += 1
        b["gw"] += pnl
    elif pnl < 0:
        b["gl"] += -pnl


def _fmt(b: dict) -> str:
    if not b["n"]:
        return "n=0"
    pf = (b["gw"] / b["gl"]) if b["gl"] else float("inf")
    return f"n={b['n']}  WR={b['wins'] / b['n'] * 100:.1f}%  PnL=${b['pnl']:+.2f}  PF={pf:.2f}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--paper-csv", type=Path, default=DATA_DIR / "logs/paper_trades.csv")
    ap.add_argument("--since", default=GATE_ERA_START,
                     help="only check rows signaled at/after this ISO timestamp "
                          "(default: the current gate era)")
    ap.add_argument("--all", action="store_true", help="ignore --since, check every resolved row")
    ap.add_argument("--out", type=Path, default=None,
                     help="onchain-relabeled copy (default: <paper>.onchain_relabel.csv)")
    ap.add_argument("--apply", action="store_true",
                     help="overwrite paper_trades.csv with onchain-verified rows (others left "
                          "untouched), with a .bak backup. Stop the bot first — auto_resolve "
                          "rewrites this file.")
    args = ap.parse_args()

    with open(args.paper_csv) as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    if "label_source" not in fieldnames:
        fieldnames.append("label_source")

    since = None if args.all else args.since
    eligible = [r for r in rows if r.get("resolved_at")
                and (since is None or r.get("signal_time", "") >= since)]
    print(f"Loaded {len(rows)} rows from {args.paper_csv}; "
          f"{len(eligible)} resolved and in scope "
          f"({'all history' if since is None else f'since {since}'})")

    condition_ids = {r["market_id"] for r in eligible if r.get("market_id")}
    print(f"Querying on-chain settlement for {len(condition_ids)} unique conditions "
          f"(payoutNumerators, {MAX_WORKERS} workers)...")
    truth = fetch_chain_truth(condition_ids)
    n_settled = sum(1 for v in truth.values() if v is not None)
    print(f"  {n_settled}/{len(condition_ids)} resolved on-chain "
          f"({len(condition_ids) - n_settled} not yet settled or ambiguous)")

    before: dict[str, dict] = {"mainline": _bucket(), "longshot": _bucket()}
    after: dict[str, dict] = {"mainline": _bucket(), "longshot": _bucket()}
    flips: dict[str, int] = {"mainline": 0, "longshot": 0}
    unresolved: dict[str, int] = {"mainline": 0, "longshot": 0}
    flip_examples: list[str] = []
    relabeled_rows: list[dict] = []

    for row in eligible:
        track = _track_of(row)
        old_pnl = float(row.get("pnl_usd") or 0.0)
        _add(before[track], old_pnl)

        cond = row.get("market_id", "")
        chain_yes = truth.get(cond)
        if chain_yes is None:
            unresolved[track] += 1
            continue

        try:
            entry = float(row["entry_price"])
            size = float(row["size_usd"])
            model_p = float(row["model_p"])
        except (KeyError, ValueError):
            unresolved[track] += 1
            continue

        old_outcome = row.get("actual_outcome") in ("1", 1, True)
        bet_wins = chain_yes if row.get("direction") == "YES" else not chain_yes
        new_pnl = _trade_pnl(size, entry, bet_wins)
        _add(after[track], new_pnl)

        if chain_yes != old_outcome:
            flips[track] += 1
            if len(flip_examples) < 15:
                flip_examples.append(
                    f"    [{track}] {row.get('market_title', '')[:60]!r}  "
                    f"dir={row.get('direction')}  weather_said={'YES' if old_outcome else 'NO'}"
                    f"  chain_said={'YES' if chain_yes else 'NO'}  "
                    f"pnl ${old_pnl:+.2f} -> ${new_pnl:+.2f}"
                )

        new_row = dict(row)
        new_row["actual_outcome"] = int(chain_yes)
        new_row["pnl_usd"] = round(new_pnl, 4)
        new_row["brier_score"] = round(_brier(model_p, chain_yes), 4)
        new_row["label_source"] = "onchain"
        relabeled_rows.append(new_row)

    print("\n══ On-chain settlement vs recorded (weather-truth) labels ══")
    for track in ("mainline", "longshot"):
        print(f"\n{track.upper()}")
        print(f"  recorded (station/grid) : {_fmt(before[track])}")
        print(f"  on-chain (verified)     : {_fmt(after[track])}"
              f"   [{unresolved[track]} not yet settled on-chain / ambiguous, excluded]")
        n_checked = before[track]["n"] - unresolved[track]
        if n_checked:
            print(f"  label flips             : {flips[track]}/{n_checked} "
                  f"({flips[track] / n_checked * 100:.1f}%)")

    if flip_examples:
        print(f"\n  Example flips (up to 15):")
        for line in flip_examples:
            print(line)

    out = args.out or args.paper_csv.with_suffix(".onchain_relabel.csv")
    relabeled_by_id = {r["trade_id"]: r for r in relabeled_rows}
    merged = [relabeled_by_id.get(r.get("trade_id"), r) for r in rows]
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(merged)
    print(f"\n  wrote on-chain-relabeled copy: {out}  "
          f"({len(relabeled_rows)} rows verified, rest unchanged)")

    if args.apply:
        ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(args.paper_csv, args.paper_csv.with_suffix(f".pre_onchain_relabel_{ts}.bak"))
        shutil.copy2(out, args.paper_csv)
        print(f"\n  APPLIED: {args.paper_csv} replaced (backup *_{ts}.bak)")
        print("  Restart the bot so /paperstats and the gate read the verified labels.")


if __name__ == "__main__":
    main()
