#!/usr/bin/env python3
"""
analyze_gaps.py — Post-collection analysis of gap_logger CSV data.

Usage: python analyze_gaps.py [logs/gaps_YYYYMMDD.csv] [--min-score 0.5]
Shows: genuine pairs (same_question_type=True), persistence stats, top candidates.
"""

import argparse
import csv
import glob
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path


def load_gaps(csv_paths: list[str]) -> list[dict]:
    rows = []
    for path in csv_paths:
        with open(path) as f:
            reader = csv.DictReader(f)
            for row in reader:
                row["_file"] = path
                rows.append(row)
    return rows


def cast(row: dict) -> dict:
    for k in ["match_score", "poly_yes", "kalshi_yes", "gap", "net_gap",
              "poly_liquidity", "poly_vol24h", "kalshi_volume", "kalshi_oi"]:
        try:
            row[k] = float(row[k])
        except (ValueError, KeyError):
            row[k] = 0.0
    for k in ["poly_has_date", "kalshi_is_timing", "same_question_type"]:
        row[k] = row.get(k, "False").lower() == "true"
    return row


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze gap logger CSV output")
    parser.add_argument("files", nargs="*", help="CSV files (default: all in logs/)")
    parser.add_argument("--min-score", type=float, default=0.5,
                        help="Minimum Jaccard score to include (default: 0.5)")
    parser.add_argument("--same-type-only", action="store_true",
                        help="Only show same_question_type=True pairs")
    args = parser.parse_args()

    csv_files = args.files or sorted(glob.glob("logs/gaps_*.csv"))
    if not csv_files:
        print("No CSV files found in logs/")
        sys.exit(1)

    print(f"Loading {len(csv_files)} file(s)...")
    rows = [cast(r) for r in load_gaps(csv_files)]
    print(f"Total rows: {len(rows)}")
    print()

    # Filter
    filtered = [r for r in rows if r["match_score"] >= args.min_score]
    if args.same_type_only:
        filtered = [r for r in filtered if r["same_question_type"]]
    print(f"After filter (score ≥ {args.min_score}"
          + (", same_question_type" if args.same_type_only else "") + "): "
          + f"{len(filtered)} rows")
    print()

    # ── Genuine pairs (same question type, score ≥ threshold) ──
    genuine = [r for r in filtered if r["same_question_type"] and not r["kalshi_is_timing"]]
    genuine.sort(key=lambda r: r["gap"], reverse=True)

    print(f"=== Genuine candidate pairs (same_question_type, no timing mismatch): {len(genuine)} ===")
    print()
    for r in genuine[:20]:
        net = r["net_gap"]
        marker = "[NET+]" if net > 0 else "      "
        print(f"{marker}  score={r['match_score']:.2f}  gap={r['gap']:.3f}  "
              f"net={net:+.3f}  P={r['poly_yes']:.4f}  K={r['kalshi_yes']:.4f}")
        print(f"         POLY:   {r['poly_title'][:80]}")
        print(f"         KALSHI: {r['kalshi_title'][:80]}")
        print(f"         liq=${r['poly_liquidity']:.0f}  kvol=${r['kalshi_volume']:.0f}  "
              f"buy={r['buy_venue']}")
        print()

    # ── Persistence: pairs seen in multiple scans ──
    if len(csv_files) > 1:
        print("\n=== Pair persistence (seen in N+ scans) ===")
        pair_counts: Counter = Counter()
        pair_gaps: dict[str, list] = defaultdict(list)
        for r in rows:
            key = (r["poly_id"], r["kalshi_id"])
            pair_counts[key] += 1
            pair_gaps[key].append(r["gap"])

        persistent = [(k, v) for k, v in pair_counts.items() if v >= 2]
        persistent.sort(key=lambda x: x[1], reverse=True)
        for (pid, kid), count in persistent[:10]:
            gaps_for_pair = pair_gaps[(pid, kid)]
            print(f"  {count}x  avg_gap={sum(gaps_for_pair)/len(gaps_for_pair):.3f}  "
                  f"pid={pid[:20]}  kid={kid[:20]}")

    # ── Summary stats ──
    print("\n=== Summary ===")
    profitable_genuine = [r for r in genuine if r["net_gap"] > 0]
    print(f"Genuine pairs:             {len(genuine)}")
    print(f"Genuine + net profitable:  {len(profitable_genuine)}")
    print(f"All pairs scanned:         {len(rows)}")
    print(f"False positive rate:       "
          + f"{(1 - len(genuine)/max(len(filtered),1)):.0%}")


if __name__ == "__main__":
    main()
