#!/usr/bin/env python3
"""Leaderboard for the production paper track vs every shadow challenger.

Reads data/logs/paper_trades.csv (production) and data/logs/shadow/<name>/
paper_trades.csv (challengers, written by weather.shadow) and prints, per track,
the numbers that decide a model change: sample size, win rate, profit factor,
PnL, and — the honest skill test the go-live gate uses — the model's Brier vs the
MARKET's Brier on the same trades. Beating climatology is easy; edge means
beating the crowd's price.

    venv/bin/python scripts/compare_tracks.py                 # DATA_DIR/logs
    venv/bin/python scripts/compare_tracks.py --log-dir PATH  # any track dir
    venv/bin/python scripts/compare_tracks.py --since 2026-08-06T12:27

This is the FORWARD read (out-of-sample, as tracks accrue). The offline in-sample
read over archived ensembles is scripts/historical_backtest.py --lambda-sweep.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from weather.config import GATE_ERA_START  # noqa: E402
from weather.paper_trader import _brier  # noqa: E402
from weather.paths import DATA_DIR  # noqa: E402


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _stats(rows: list[dict]) -> dict | None:
    """Track stats over RESOLVED, non-longshot rows. Market P(YES) is recovered
    from entry_price+direction (paper stores entry_price = market_p for YES,
    1−market_p for NO), so model Brier and market Brier are on identical events."""
    res = [r for r in rows
           if r.get("actual_outcome") in ("0", "1")
           and (r.get("scan_source") or "") != "longshot"
           and _num(r.get("model_p")) is not None]
    if not res:
        return None
    pnls, mb, kb, wins = [], [], [], 0
    for r in res:
        outcome = int(r["actual_outcome"])          # 1 = YES happened
        model_p = _num(r["model_p"])                 # model P(YES)
        ep = _num(r.get("entry_price"))
        direction = r.get("direction", "YES")
        market_p = ep if direction == "YES" else (1.0 - ep if ep is not None else None)
        pnl = _num(r.get("pnl_usd"))
        if pnl is not None:
            pnls.append(pnl)
            wins += pnl > 0
        mb.append(_brier(model_p, bool(outcome)))
        if market_p is not None:
            kb.append(_brier(market_p, bool(outcome)))
    gp = sum(p for p in pnls if p > 0)
    gl = -sum(p for p in pnls if p <= 0)
    return {
        "n": len(res),
        "wr": wins / len(pnls) * 100 if pnls else 0.0,
        "pf": (gp / gl) if gl > 0 else float("inf"),
        "pnl": sum(pnls),
        "model_brier": sum(mb) / len(mb) if mb else None,
        "market_brier": sum(kb) / len(kb) if kb else None,
    }


def _load(path: Path, since: str) -> list[dict]:
    if not path.exists():
        return []
    with open(path) as f:
        return [r for r in csv.DictReader(f) if str(r.get("signal_time", "")) >= since]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log-dir", default=str(DATA_DIR / "logs"))
    ap.add_argument("--since", default=GATE_ERA_START,
                    help="only signals at/after this ISO instant (default: gate era start)")
    args = ap.parse_args()
    log_dir = Path(args.log_dir)

    tracks: list[tuple[str, Path]] = [("production", log_dir / "paper_trades.csv")]
    shadow_root = log_dir / "shadow"
    if shadow_root.is_dir():
        for d in sorted(shadow_root.iterdir()):
            if (d / "paper_trades.csv").exists():
                tracks.append((d.name, d / "paper_trades.csv"))

    print(f"Track comparison — signals since {args.since}\n")
    header = f"{'track':<16}{'n':>5}{'WR':>7}{'PF':>7}{'PnL':>10}{'mBrier':>9}{'mktBrier':>10}  skill"
    print(header)
    print("-" * len(header))
    baseline = None
    for name, path in tracks:
        s = _stats(_load(path, args.since))
        if not s:
            print(f"{name:<16}{'—':>5}  (no resolved trades yet)")
            continue
        mb = s["model_brier"]
        kb = s["market_brier"]
        skill = ""
        if mb is not None and kb is not None:
            skill = f"model {'BEATS' if mb < kb else 'loses to'} market"
        pf = "inf" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
        print(f"{name:<16}{s['n']:>5}{s['wr']:>6.0f}%{pf:>7}{s['pnl']:>+10.2f}"
              f"{(mb if mb is not None else 0):>9.4f}{(kb if kb is not None else 0):>10.4f}  {skill}")
        if name == "production":
            baseline = s
    if baseline and baseline["model_brier"] is not None:
        print(f"\nBaseline (production) model Brier: {baseline['model_brier']:.4f} — "
              f"a challenger is better if its model Brier is LOWER at comparable n.")


if __name__ == "__main__":
    main()
