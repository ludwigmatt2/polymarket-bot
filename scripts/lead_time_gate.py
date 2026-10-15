"""Pre-registered gate: do signals within 12h of resolution really outperform?

Context (2026-10-15, in-sample). By hours from signal_time to resolution_date,
the < 12h bucket was the only consistently good one in BOTH tracks:
  live  < 12h: n=48,  60% win, +$51.53, ROI +34%   (>= 12h: n=163, -$39.08, ~-7%)
  paper < 12h: n=161, 58% win, +$1,145, ROI +36%   (12-24h +2%, 24-36h +17%)
Plausible mechanism: close to resolution the intraday running observation
(running_obs_c) carries real information; far out only the forecast does. But
this was found by slicing one dataset five ways after the fact, and the
12h cutoff is a round number picked from the same table — exactly the kind of
result this project's operating rule says not to act on at this n.

THE GATE (locked 2026-10-15 before scoring new data — do not loosen):
  - Only rows with signal_time >= LOCK_DATE. Two independent record sets:
    LIVE fills (live_trades.csv) and PAPER (paper_trades.csv, entry >= 0.10,
    no precipitation — the sub-$0.10 lottery tickets are a different game).
  - lead = resolution_date - signal_time in hours. SHORT is lead < CUT_HOURS
    (12), fixed. No other cutoff is allowed to decide anything.
  - Answerable only when EACH set has n >= MIN_N (250) resolved rows with
    >= MIN_SHORT (50) in the short bucket.
  - Metric: ROI(short) - ROI(long), ROI = pnl / stake. Day-clustered bootstrap
    P(diff > 0): paper is the larger sample (>= 0.90 needed), live is the real
    money (>= 0.80 needed). Both diffs must be > 0 and live ROI(short) > 0.
  - AMENDED 2026-10-15 (same day, before any post-lock row had resolved,
    n=0): ROI is NET of the Polymarket taker fee (weather/fees.py,
    0.05*p*(1-p) per share, measured on-chain) for both sets — neither CSV
    subtracts it. The in-sample table above is gross and is context only.
  - Action on PROCEED is also pre-stated: a smaller stake multiplier for lead
    >= 12h (env-overridable, shadow-evaluated first), NOT blanket exclusion.

Run: venv/bin/python scripts/lead_time_gate.py
"""
from __future__ import annotations

import csv
import random
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from weather.fees import taker_fee_usd  # noqa: E402
from weather.paths import DATA_DIR  # noqa: E402

LIVE_LOG = DATA_DIR / "logs" / "live_trades.csv"
PAPER_LOG = DATA_DIR / "logs" / "paper_trades.csv"
LOCK_DATE = "2026-10-15T00:00:00+00:00"  # do not move this once set
CUT_HOURS = 12.0
MIN_N = 250
MIN_SHORT = 50
P_LIVE = 0.80
P_PAPER = 0.90
PAPER_MIN_ENTRY = 0.10
BOOT_N = 5000
BOOT_SEED = 1


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _lead_hours(signal_time: str, resolution_date: str) -> float | None:
    try:
        return (datetime.fromisoformat(resolution_date) - datetime.fromisoformat(signal_time)
                ).total_seconds() / 3600.0
    except (TypeError, ValueError):
        return None


def _load(path: Path, *, live: bool) -> list[dict]:
    if not path.exists():
        sys.exit(f"Missing {path}\n  On the VPS: sudo -u bot env "
                 "RAILWAY_VOLUME_MOUNT_PATH=/opt/polymarket-bot/data venv/bin/python "
                 "scripts/lead_time_gate.py")
    out = []
    with path.open() as f:
        for r in csv.DictReader(f):
            if live:
                if r.get("order_status") != "matched":
                    continue
                size, px = _num(r.get("filled_size")), _num(r.get("filled_price"))
                stake = size * px if size and px else None
            else:
                if _num(r.get("entry_price")) is None or _num(r["entry_price"]) < PAPER_MIN_ENTRY:
                    continue
                if r.get("metric") == "precipitation_sum":
                    continue
                px = _num(r["entry_price"])
                stake = _num(r.get("size_usd"))
            pnl = _num(r.get("pnl_usd"))
            if not stake or pnl is None or r.get("actual_outcome", "") == "":
                continue
            pnl -= taker_fee_usd(stake, px)
            lead = _lead_hours(r.get("signal_time", ""), r.get("resolution_date", ""))
            if lead is None:
                continue
            out.append({"stake": stake, "pnl": pnl, "lead": lead, "t": r["signal_time"]})
    return out


def load_live(path: Path = LIVE_LOG) -> list[dict]:
    return _load(path, live=True)


def load_paper(path: Path = PAPER_LOG) -> list[dict]:
    return _load(path, live=False)


def _roi(rows: list[dict]) -> float | None:
    c = sum(r["stake"] for r in rows)
    return sum(r["pnl"] for r in rows) / c if c else None


def _diff(rows: list[dict]) -> float | None:
    s = _roi([r for r in rows if r["lead"] < CUT_HOURS])
    l = _roi([r for r in rows if r["lead"] >= CUT_HOURS])
    return None if s is None or l is None else s - l


def _boot_p(rows: list[dict]) -> float:
    by_day: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_day[r["t"][:10]].append(r)
    days = list(by_day.values())
    rng = random.Random(BOOT_SEED)
    ok = tot = 0
    for _ in range(BOOT_N):
        d = _diff([r for dd in (rng.choice(days) for _ in days) for r in dd])
        if d is not None:
            tot += 1
            ok += d > 0
    return ok / tot if tot else 0.0


def evaluate_set(rows: list[dict]) -> dict:
    short = [r for r in rows if r["lead"] < CUT_HOURS]
    res = {"n": len(rows), "n_short": len(short), "answerable": False,
           "roi_short": _roi(short), "roi_long": _roi([r for r in rows if r["lead"] >= CUT_HOURS]),
           "diff": _diff(rows)}
    if len(rows) >= MIN_N and len(short) >= MIN_SHORT and res["diff"] is not None:
        res["answerable"] = True
        res["boot_p"] = _boot_p(rows)
    return res


def verdict(live: dict, paper: dict) -> dict:
    out = {"answerable": live["answerable"] and paper["answerable"], "proceed": False}
    if out["answerable"]:
        out["proceed"] = (live["diff"] > 0 and paper["diff"] > 0 and (live["roi_short"] or 0) > 0
                          and live["boot_p"] >= P_LIVE and paper["boot_p"] >= P_PAPER)
    return out


def _fmt(label: str, r: dict) -> None:
    def pct(x):
        return "n/a" if x is None else f"{x*100:+.0f}%"
    p = f"  P(diff>0)={r['boot_p']:.2f}" if "boot_p" in r else ""
    print(f"  {label}: n={r['n']:4d} short={r['n_short']:3d}  ROI short {pct(r['roi_short'])}  "
          f"long {pct(r['roi_long'])}  diff {pct(r['diff'])}{p}")


def main() -> None:
    print(f"Lead-time gate — is signal-to-resolution < {CUT_HOURS:.0f}h really better?\n")
    live = evaluate_set([r for r in load_live() if r["t"] >= LOCK_DATE])
    paper = evaluate_set([r for r in load_paper() if r["t"] >= LOCK_DATE])
    print(f"GATE (locked {LOCK_DATE}): each set needs n>={MIN_N} with >={MIN_SHORT} short. So far:")
    _fmt("LIVE ", live)
    _fmt("PAPER", paper)
    v = verdict(live, paper)
    if not v["answerable"]:
        print("\nVERDICT: not answerable yet. Nothing to turn on — both logs already record "
              "everything this needs. Do NOT act on the in-sample table in the docstring.")
        return
    if v["proceed"]:
        print(f"\nVERDICT: PROCEED — short-lead edge replicated out of sample in both sets "
              f"(live P>={P_LIVE}, paper P>={P_PAPER}). Next: a smaller stake multiplier for "
              f"lead >= {CUT_HOURS:.0f}h as an env-overridable switch, shadow-evaluated first.")
    else:
        print("\nVERDICT: do NOT re-weight by lead time — the in-sample gap did not replicate "
              "with the required confidence in both sets.")


if __name__ == "__main__":
    main()
