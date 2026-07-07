"""Bulk-relabel historical paper trades against STATION truth and regenerate the
calibration log.

Why: every paper outcome before Phase 1 (PR #33, Jul 5) was labeled by Open-Meteo
grid reanalysis, which disagreed with Polymarket's station-based on-chain
resolution on ~33% of outcomes (phase-3 backtest, PR #36). Those labels sit in
paper_trades.csv (the go-live gate's dataset) and calibration_log.csv (the
production calibrator's training set). This script re-scores every resolved
temperature trade on the station that actually settles its market (WU → IEM,
whole-degree rounding in the market's unit — weather/station_truth.py) and:

  1. prints the honest before/after stats (WR / PF / PnL / label flips),
  2. writes a relabeled copy of the paper log (--out),
  3. writes a regenerated calibration log built ONLY from rows with a stored
     raw_p and a station-verified outcome (--calib-out),
  4. optionally applies both over the live files with .bak backups (--apply).
     Stop the bot before --apply: auto_resolve rewrites the same files.

Rows whose market can't be tied to a registered station (Madrid, Toronto, …)
or whose station has no data for the day are left unchanged and EXCLUDED from
the regenerated calibration log — an unverifiable label is worse than none.

Run on the VPS (full history):
  RAILWAY_VOLUME_MOUNT_PATH=/opt/polymarket-bot/data venv/bin/python \
      scripts/relabel_paper_truth.py            # dry-run report
  ... --apply                                   # write relabeled files
"""

from __future__ import annotations

import argparse
import csv
import re
import shutil
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from weather.paper_trader import _brier, _local_event_date, _trade_pnl  # noqa: E402
from weather.paths import DATA_DIR  # noqa: E402
from weather.station_truth import station_outcome  # noqa: E402

# The station each city's Polymarket temperature market resolves on — verified
# against live Gamma rules text (Jul 7 2026 audit + PR #42). Legacy rows carry
# only the city geocode, so the market title is the join key. Units are the
# fallback when the title itself doesn't say °F/°C (it almost always does).
CITY_STATIONS: dict[str, tuple[str, str, str]] = {
    "New York": ("KLGA", "US", "F"),
    "NYC": ("KLGA", "US", "F"),
    "Miami": ("KMIA", "US", "F"),
    "Atlanta": ("KATL", "US", "F"),
    "Dallas": ("KDAL", "US", "F"),      # Love Field — NOT KDFW
    "Paris": ("LFPB", "FR", "C"),
    "Seoul": ("RKSI", "KR", "C"),
    "Hong Kong": ("VHHH", "HK", "C"),
    "Tel Aviv": ("LLBG", "IL", "C"),
    "London": ("EGLC", "GB", "F"),      # resolves in °F despite being a UK market
    "Tokyo": ("RJTT", "JP", "C"),
}
# Rows logged while the registry briefly pointed Dallas at DFW.
_ICAO_REMAP = {"KDFW": "KDAL"}

_TITLE_UNIT = re.compile(r"°\s*([CF])\b")
_TEMP_METRICS = ("temperature_2m_max", "temperature_2m_min")


def station_for_row(row: dict) -> tuple[str, str, str] | None:
    """(icao, country, unit) for a paper-trade row, or None if unmappable.
    Prefers the row's own station tag (post-Phase-1 rows), remapping known-wrong
    ICAOs; falls back to matching the market title against CITY_STATIONS."""
    icao = _ICAO_REMAP.get(row.get("station_icao") or "", row.get("station_icao") or "")
    unit_row = (row.get("resolve_unit") or "").upper()
    title = row.get("market_title") or ""
    m = _TITLE_UNIT.search(title)
    unit_title = m.group(1).upper() if m else ""

    if icao:
        for _city, (c_icao, country, c_unit) in CITY_STATIONS.items():
            if c_icao == icao:
                return icao, country, unit_row or unit_title or c_unit
        return icao, row.get("station_country") or "", unit_row or unit_title or "F"

    for city, (c_icao, country, c_unit) in CITY_STATIONS.items():
        if city in title:
            return c_icao, country, unit_title or c_unit
    return None


def relabel(rows: list[dict], sleep_s: float = 0.25) -> dict:
    """Re-score every resolved temperature row in place (station-truth labels).
    Returns a stats dict; rows gain 'label_source' ('station' | original '')."""
    cache: dict = {}
    stats = Counter()
    flips_by_city: Counter = Counter()
    old = {"pnl": 0.0, "wins": 0, "gw": 0.0, "gl": 0.0, "n": 0}
    new = {"pnl": 0.0, "wins": 0, "gw": 0.0, "gl": 0.0, "n": 0}

    for row in rows:
        if row.get("actual_outcome") in (None, "", "None"):
            continue
        if row.get("metric") not in _TEMP_METRICS:
            stats["skipped_non_temp"] += 1
            continue
        st = station_for_row(row)
        if st is None:
            stats["skipped_no_station"] += 1
            continue
        icao, country, unit = st
        try:
            res_dt = datetime.fromisoformat(row["resolution_date"])
            threshold = float(row["threshold"])
            threshold_high = float(row["threshold_high"]) if row.get("threshold_high") else None
            entry = float(row["entry_price"])
            size = float(row["size_usd"])
            model_p = float(row["model_p"])
        except (KeyError, ValueError):
            stats["skipped_bad_row"] += 1
            continue
        w_dir = row.get("weather_direction") or "above"
        event_date = _local_event_date(res_dt, row.get("location_tz") or "UTC")

        n_probes = len(cache)
        outcome, src, _val = station_outcome(
            icao, country, unit, event_date, row["metric"],
            threshold, threshold_high, w_dir, cache=cache,
        )
        if len(cache) > n_probes:  # fresh network probe → be polite to WU
            time.sleep(sleep_s)
        if outcome is None:
            stats["skipped_no_data"] += 1
            continue

        old_outcome = row["actual_outcome"] in ("1", 1, True)
        old_pnl = float(row.get("pnl_usd") or 0.0)
        bet_wins = outcome if row.get("direction") == "YES" else not outcome
        pnl = _trade_pnl(size, entry, bet_wins)

        old["n"] += 1; old["pnl"] += old_pnl
        old["wins"] += old_pnl > 0
        old["gw"] += max(old_pnl, 0.0); old["gl"] += max(-old_pnl, 0.0)
        new["n"] += 1; new["pnl"] += pnl
        new["wins"] += pnl > 0
        new["gw"] += max(pnl, 0.0); new["gl"] += max(-pnl, 0.0)

        if outcome != old_outcome:
            stats["flipped"] += 1
            city = next((c for c in CITY_STATIONS if c in (row.get("market_title") or "")), icao)
            flips_by_city[city] += 1
        stats["relabeled"] += 1
        stats[f"src_{src}"] += 1

        row["actual_outcome"] = int(outcome)
        row["pnl_usd"] = round(pnl, 4)
        row["brier_score"] = round(_brier(model_p, outcome), 4)
        row["label_source"] = "station"

    # Rebuild cumulative columns over ALL resolved rows in file order
    cum_pnl = cum_brier = 0.0
    for row in rows:
        if row.get("pnl_usd") not in (None, ""):
            cum_pnl += float(row["pnl_usd"])
            row["cumulative_pnl"] = round(cum_pnl, 4)
        if row.get("brier_score") not in (None, ""):
            cum_brier += float(row["brier_score"])
            row["cumulative_brier"] = round(cum_brier, 4)

    return {"stats": stats, "flips_by_city": flips_by_city, "old": old, "new": new}


def calibration_rows(rows: list[dict]) -> list[dict]:
    """Calibration observations from station-relabeled rows that carry raw_p —
    the scale the calibrator is applied to (Phase-0 rule). Rows without raw_p
    (pre-Phase-0) or without a station label are excluded: mixed scales and
    unverifiable outcomes are exactly the two poisons being removed."""
    out = []
    for row in rows:
        if row.get("label_source") != "station":
            continue
        raw = row.get("raw_p")
        if raw in (None, ""):
            continue
        out.append({
            "logged_at": row.get("resolved_at") or datetime.utcnow().isoformat(),
            "model_p": round(float(raw), 4),
            "actual_outcome": int(row["actual_outcome"]),
            "direction": row.get("weather_direction") or "",
        })
    return out


def _fmt(b: dict) -> str:
    if not b["n"]:
        return "n=0"
    pf = (b["gw"] / b["gl"]) if b["gl"] else float("inf")
    return (f"n={b['n']}  WR={b['wins'] / b['n'] * 100:.1f}%  "
            f"PnL=${b['pnl']:.2f}  PF={pf:.2f}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--paper-csv", type=Path, default=DATA_DIR / "logs/paper_trades.csv")
    ap.add_argument("--out", type=Path, default=None,
                    help="relabeled copy (default: <paper>.station_relabel.csv)")
    ap.add_argument("--calib-out", type=Path, default=None,
                    help="regenerated calibration log (default: <logs>/calibration_log.regenerated.csv)")
    ap.add_argument("--apply", action="store_true",
                    help="overwrite paper_trades.csv AND calibration_log.csv (with .bak backups). "
                         "Stop the bot first — auto_resolve rewrites these files.")
    ap.add_argument("--sleep", type=float, default=0.25, help="seconds between fresh WU probes")
    args = ap.parse_args()

    with open(args.paper_csv) as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    if "label_source" not in fieldnames:
        fieldnames.append("label_source")

    print(f"Loaded {len(rows)} rows from {args.paper_csv}")
    r = relabel(rows, sleep_s=args.sleep)
    s = r["stats"]

    print("\n══ Relabel report (resolved temperature trades) ══")
    print(f"  relabeled on station truth : {s['relabeled']}")
    print(f"  labels flipped             : {s['flipped']} "
          f"({s['flipped'] / s['relabeled'] * 100:.1f}%)" if s["relabeled"] else "")
    print(f"  skipped — no station       : {s['skipped_no_station']} (unregistered city)")
    print(f"  skipped — no station data  : {s['skipped_no_data']}")
    print(f"  skipped — non-temp         : {s['skipped_non_temp']}")
    print(f"  truth sources              : " +
          ", ".join(f"{k[4:]}={v}" for k, v in s.items() if k.startswith("src_")))
    if r["flips_by_city"]:
        print("  flips by city              : " +
              ", ".join(f"{c}={n}" for c, n in r["flips_by_city"].most_common()))
    print(f"\n  BEFORE (grid labels)   : {_fmt(r['old'])}")
    print(f"  AFTER  (station labels): {_fmt(r['new'])}")

    calib = calibration_rows(rows)
    dirs = Counter(c["direction"] for c in calib)
    print(f"\n  regenerated calibration obs: {len(calib)}  ({dict(dirs)})")

    out = args.out or args.paper_csv.with_suffix(".station_relabel.csv")
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"  wrote relabeled paper log  : {out}")

    calib_out = args.calib_out or args.paper_csv.parent / "calibration_log.regenerated.csv"
    with open(calib_out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["logged_at", "model_p", "actual_outcome", "direction"])
        w.writeheader()
        w.writerows(calib)
    print(f"  wrote calibration log      : {calib_out}")

    if args.apply:
        live_calib = args.paper_csv.parent / "calibration_log.csv"
        ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(args.paper_csv, args.paper_csv.with_suffix(f".pre_relabel_{ts}.bak"))
        if live_calib.exists():
            shutil.copy2(live_calib, live_calib.with_suffix(f".pre_relabel_{ts}.bak"))
        shutil.copy2(out, args.paper_csv)
        shutil.copy2(calib_out, live_calib)
        print(f"\n  APPLIED: {args.paper_csv} and {live_calib} replaced (backups *_{ts}.bak)")
        print("  Restart the bot so the calibrator refits on the new log.")


if __name__ == "__main__":
    main()
