"""Walk-forward test of a per-station MOSMIX bias correction (Phase 3 follow-up
to the T1 verdict: EGLC/KATL/RJTT/RKSI show real systematic bias).

Question: would subtracting a learned per-(station, kind) bias from MOSMIX's mean
have improved it — judged ONLY on days the estimator hadn't seen? Expanding
window by target_day: the correction applied to day D is fit on rows with
target_day < D, so there is no look-ahead. Prints MAE and mean-error (bias)
before/after, overall and per station, and the PIT z-mean after correction.

    venv/bin/python scripts/mosmix_bias_eval.py [--k 10] [--min-n 15] [--by-lead]

--k       shrinkage strength: applied bias = n/(n+k) * mean_error
--min-n   fewer prior DAYS than this -> no correction (stands down)
--by-lead key on (station, kind, lead bucket) instead of (station, kind)

Reuses mosmix_backcheck's truth join. n is printed everywhere; this is a
forecast-error read, not a trading-edge read, but the same caution applies.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import mosmix_backcheck as bc  # noqa: E402
from weather.mosmix_bias import shrunk_bias  # noqa: E402


CACHE_PATH = Path("/tmp/mosmix_truth_cache.json")


def _load_cache() -> dict:
    try:
        raw = json.loads(CACHE_PATH.read_text())
        return {tuple(k.split("|")): v for k, v in raw.items()}
    except (OSError, ValueError):
        return {}


def _save_cache(cache: dict) -> None:
    CACHE_PATH.write_text(json.dumps({"|".join(k): v for k, v in cache.items()}))


def load_resolved() -> list[dict]:
    rows = bc._load_rows()
    today = datetime.now(timezone.utc).date()
    cache = _load_cache()
    out = []
    for r in rows:
        day = date.fromisoformat(r["target_day"])
        if day >= today or not r["mosmix_mean_c"]:
            continue
        k = (r["icao"], r["target_day"])
        if k not in cache:
            cache[k] = bc._truth_row(r["icao"], day)
            if len(cache) % 25 == 0:
                _save_cache(cache)
        t = (cache[k] or {}).get(r["kind"])
        if t is None:
            continue
        lead = int(r["lead_days"])
        bucket = next((b for b, rng in bc.LEAD_BUCKETS if lead in rng), None)
        if bucket is None:
            continue
        out.append({
            "icao": r["icao"], "kind": r["kind"], "day": r["target_day"],
            "bucket": bucket, "truth": t,
            "mos": float(r["mosmix_mean_c"]),
            "sigma": float(r["mosmix_sigma_c"]) if r["mosmix_sigma_c"] else None,
            "om_corr": float(r["om_corrected_mean_c"]) if r["om_corrected_mean_c"] else None,
            "snap": r["snapshot_at"],
        })
    _save_cache(cache)
    return out


def walk_forward(rows: list[dict], k: float, min_n: int, by_lead: bool) -> list[dict]:
    """Causal by construction: the correction for a row uses only errors from
    target days strictly BEFORE the row's SNAPSHOT date — the latest truth that
    was actually final when that forecast was issued. (Keying on target_day
    instead would hand a 6-day-lead forecast the truth of days it could not
    have seen; the first version of this script did exactly that.)"""
    key = (lambda r: (r["icao"], r["kind"], r["bucket"])) if by_lead else (lambda r: (r["icao"], r["kind"]))
    # per key: {target_day: [errors]} so each DAY counts once in n, not each lead
    hist: dict[tuple, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        hist[key(r)][r["day"]].append(r["mos"] - r["truth"])
    res = []
    for r in rows:
        snap_day = r["snap"][:10]
        days = {d: v for d, v in hist[key(r)].items() if d < snap_day}
        est = shrunk_bias(days, k, min_n)  # shared with the live shadow track
        n = len(days)                      # independent days, not correlated rows
        adj = est[0] if est else 0.0
        res.append({**r, "n_prior": n, "adj": adj,
                    "err0": r["mos"] - r["truth"],
                    "err1": r["mos"] - adj - r["truth"]})
    return res


def summarize(res: list[dict]) -> None:
    def line(name, rs):
        if not rs:
            return
        m0 = statistics.mean(abs(r["err0"]) for r in rs)
        m1 = statistics.mean(abs(r["err1"]) for r in rs)
        b0 = statistics.mean(r["err0"] for r in rs)
        b1 = statistics.mean(r["err1"] for r in rs)
        zs0 = [(-r["err0"]) / r["sigma"] for r in rs if r["sigma"]]
        zs1 = [(-r["err1"]) / r["sigma"] for r in rs if r["sigma"]]
        z = f"{statistics.mean(zs0):+.2f}->{statistics.mean(zs1):+.2f}" if zs0 else "n/a"
        print(f"{name:14} n={len(rs):>4}  MAE {m0:.3f}->{m1:.3f} ({m1 - m0:+.3f})  "
              f"bias {b0:+.2f}->{b1:+.2f}  z-mean {z}")

    active = [r for r in res if r["adj"] != 0.0]
    print(f"scored {len(res)} rows; correction active on {len(active)} "
          f"({100 * len(active) / max(1, len(res)):.0f}%)\n")
    line("ALL (active)", active)
    line("ALL (every row)", res)
    print()
    by = defaultdict(list)
    for r in active:
        by[r["icao"]].append(r)
    improved = 0
    for icao in sorted(by):
        line(icao, by[icao])
        rs = by[icao]
        if statistics.mean(abs(r["err1"]) for r in rs) < statistics.mean(abs(r["err0"]) for r in rs):
            improved += 1
    print(f"\nstations improved: {improved}/{len(by)}")
    # does it hurt where MOSMIX was already honest?
    print("\nby lead bucket (active rows):")
    for b, _ in bc.LEAD_BUCKETS:
        line(b, [r for r in active if r["bucket"] == b])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=float, default=10.0)
    ap.add_argument("--min-n", type=int, default=7, help="min prior DAYS (not rows)")
    ap.add_argument("--by-lead", action="store_true")
    a = ap.parse_args()
    rows = load_resolved()
    print(f"resolved+truthed rows: {len(rows)}  (k={a.k}, min_n={a.min_n}, by_lead={a.by_lead})")
    if not rows:
        return
    summarize(walk_forward(rows, a.k, a.min_n, a.by_lead))


if __name__ == "__main__":
    main()
