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


# Above this mean |Δ model_p| between production and prod_mirror, the mirror is a
# different model rather than a reproduction and the baseline can't be trusted.
# Real divergence measured Sep 2026 (cold shadow calibrators) was 0.061.
FAITHFUL_MAX_DP = 0.02


def _faithfulness(paths: dict[str, Path], since: str) -> dict | None:
    """How closely prod_mirror reproduces production on the markets BOTH scored.

    The harness is only trustworthy if its baseline is faithful; prod_mirror
    exists for exactly that check (see weather/shadow.py DEFAULT_SPECS). This
    surfaces the drift automatically instead of leaving it to be noticed by hand.
    Returns None when either track is missing or they share no resolved market.
    """
    if "production" not in paths or "prod_mirror" not in paths:
        return None

    def _by_market(p: Path) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for r in _load(p, since):
            if r.get("actual_outcome") in ("0", "1") and _num(r.get("model_p")) is not None:
                out.setdefault(r.get("market_id", ""), r)
        return out

    prod, mirror = _by_market(paths["production"]), _by_market(paths["prod_mirror"])
    shared = set(prod) & set(mirror)
    if not shared:
        return None
    deltas = [abs(_num(prod[m]["model_p"]) - _num(mirror[m]["model_p"])) for m in shared]
    return {
        "shared": len(shared),
        "mean_dp": sum(deltas) / len(deltas),
        "max_dp": max(deltas),
        "prod_only": len(set(prod) - set(mirror)),
        "mirror_only": len(set(mirror) - set(prod)),
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

    # Fail loudly on a wrong log dir. DATA_DIR falls back to the repo root unless
    # RAILWAY_VOLUME_MOUNT_PATH is set — the systemd unit sets it via EnvironmentFile,
    # but a hand-run `sudo -u bot venv/bin/python` does NOT, so this used to read an
    # empty /opt/polymarket-bot/logs and print "no resolved trades yet" for every
    # track. That reads as "nothing is running" when everything is fine.
    prod_log = log_dir / "paper_trades.csv"
    if not prod_log.exists():
        sys.exit(
            f"No paper_trades.csv under {log_dir}\n"
            "  The log dir is DATA_DIR/logs, and DATA_DIR falls back to the repo root\n"
            "  unless RAILWAY_VOLUME_MOUNT_PATH is set. On the VPS run:\n"
            "    sudo -u bot env RAILWAY_VOLUME_MOUNT_PATH=/opt/polymarket-bot/data \\\n"
            "      venv/bin/python scripts/compare_tracks.py\n"
            "  or pass --log-dir explicitly."
        )

    tracks: list[tuple[str, Path]] = [("production", prod_log)]
    shadow_root = log_dir / "shadow"
    if shadow_root.is_dir():
        for d in sorted(shadow_root.iterdir()):
            if (d / "paper_trades.csv").exists():
                tracks.append((d.name, d / "paper_trades.csv"))
    else:
        print(f"warning: no shadow/ under {log_dir} — showing production only. "
              "Shadow tracks need SHADOW_TRACKS_ENABLED=1 on the service.\n",
              file=sys.stderr)

    print(f"Track comparison — signals since {args.since}\n")

    fid = _faithfulness(dict(tracks), args.since)
    if fid and fid["mean_dp"] > FAITHFUL_MAX_DP:
        print(f"!! prod_mirror has DRIFTED from production: mean |Δ model_p| "
              f"{fid['mean_dp']:.3f} (max {fid['max_dp']:.3f}) over {fid['shared']} "
              f"shared markets; {fid['mirror_only']} markets it traded and production "
              f"did not, {fid['prod_only']} the reverse.\n"
              f"   prod_mirror's job is to REPRODUCE production, so while this holds "
              f"the challenger columns below\n"
              f"   are comparable to each other but NOT to production — do not promote "
              f"anything off them.\n", file=sys.stderr)

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
