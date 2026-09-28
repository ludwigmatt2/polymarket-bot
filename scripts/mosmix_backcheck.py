"""Phase 3 T1 go/no-go read (docs/PHASE3_STATION_FORECAST_PLAN.md).

Joins scripts/mosmix_snapshot.py's growing archive (DATA_DIR/logs/mosmix_archive.csv)
against IEM station truth for every target_day that has since resolved, and
reports, per station and by lead-time bucket:
  - MAE / bias for MOSMIX, the raw Open-Meteo ensemble mean, and our current
    skill-corrected mean
  - MOSMIX's PIT z-score = (truth - mosmix_mean) / mosmix_sigma — should be
    ~N(0,1) if E_TTT is honest; a std far from 1 means the uncertainty is
    mis-calibrated and every downstream probability would be over/under-
    confident (see mosmix_client.py's daily_extreme docstring)

Gate (from the plan, do not soften): proceed to T2 only if MOSMIX beats the
current pipeline on station MAE AND its sigma is within ~20% of honest, on a
MAJORITY of stations. "Merely equal" is a no. Applies segmented by lead-time
bucket, not pooled — see longshot_price_leak.md on what pooling hides.

n IS THE STORY. This script prints it prominently and refuses to print a
verdict below a real sample — see edge_decay_sep2026.md: "no model decision
under ~250 resolved trades; any 2-week window can show anything." A forecast
MAE read needs less than a trading-edge read but still enough that a handful
of unusually easy/hard days can't flip the number; treat anything under this
script's own printed thresholds as "not yet answerable", not as a "no".

Run: venv/bin/python scripts/mosmix_backcheck.py
"""
from __future__ import annotations

import csv
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from weather import iem_client  # noqa: E402
from weather.paths import DATA_DIR  # noqa: E402

ARCHIVE_PATH = DATA_DIR / "logs" / "mosmix_archive.csv"
# Below this many (station, day) pairs in a bucket, print the number but don't
# let it drive a verdict — see module docstring.
MIN_N_FOR_VERDICT = 40
LEAD_BUCKETS = [("0-1d", range(0, 2)), ("2-3d", range(2, 4)), ("4-6d", range(4, 7))]


def _truth_row(icao: str, day: date) -> dict[str, float] | None:
    """{"max": degC, "min": degC} for one IEM call — daily_maxmin already
    returns both, so callers must cache by (icao, day) and read out the kind
    they need, rather than re-fetching per kind (that doubled this script's
    IEM traffic and runtime for every resolved station/day)."""
    r = iem_client.daily_maxmin(icao, day)
    if not r:
        return None
    out = {}
    for kind in ("max", "min"):
        v = r.get(f"{kind}_f")
        if v is not None:
            out[kind] = iem_client.f_to_c(v)
    return out or None


def _load_rows() -> list[dict]:
    if not ARCHIVE_PATH.exists():
        print(f"No archive yet at {ARCHIVE_PATH} — run scripts/mosmix_snapshot.py first "
              "(and let it run daily for a while; one snapshot proves nothing).")
        sys.exit(0)
    with ARCHIVE_PATH.open() as f:
        return list(csv.DictReader(f))


def main() -> None:
    rows = _load_rows()
    today = datetime.now(timezone.utc).date()

    # resolved[(icao, kind)] -> list of dicts with mos/om_raw/om_corr errors + z
    per_station_lead: dict[tuple[str, str], list[dict]] = defaultdict(list)
    truth_cache: dict[tuple[str, str], dict[str, float] | None] = {}
    n_pending = n_no_truth = n_resolved = 0

    for row in rows:
        day = date.fromisoformat(row["target_day"])
        if day >= today:
            n_pending += 1
            continue
        icao, kind = row["icao"], row["kind"]
        tkey = (icao, row["target_day"])
        if tkey not in truth_cache:
            truth_cache[tkey] = _truth_row(icao, day)
        truth_row = truth_cache[tkey]
        truth = truth_row.get(kind) if truth_row else None
        if truth is None:
            n_no_truth += 1
            continue
        n_resolved += 1

        lead = int(row["lead_days"])
        bucket = next((b for b, rng in LEAD_BUCKETS if lead in rng), None)
        if bucket is None:
            continue

        rec = {"truth": truth, "lead": lead}
        if row["mosmix_mean_c"]:
            mos_mean = float(row["mosmix_mean_c"])
            rec["mos_err"] = mos_mean - truth
            if row["mosmix_sigma_c"]:
                sigma = float(row["mosmix_sigma_c"])
                if sigma > 0:
                    rec["mos_z"] = (truth - mos_mean) / sigma
        if row["om_raw_mean_c"]:
            rec["om_raw_err"] = float(row["om_raw_mean_c"]) - truth
        if row["om_corrected_mean_c"]:
            rec["om_corr_err"] = float(row["om_corrected_mean_c"]) - truth

        per_station_lead[(icao, bucket)].append(rec)

    print(f"archive: {len(rows)} rows total | resolved+truthed {n_resolved} | "
          f"future/unresolved {n_pending} | truth unavailable {n_no_truth}\n")

    if n_resolved == 0:
        print("Nothing resolved yet. Come back once scripts/mosmix_snapshot.py has been "
              "running for a few days.")
        return

    def vals(recs: list[dict], key: str) -> list[float]:
        return [r[key] for r in recs if key in r]

    def mae(recs: list[dict], key: str) -> tuple[float, int] | None:
        v = vals(recs, key)
        return (statistics.mean(abs(x) for x in v), len(v)) if v else None

    mosmix_wins = mosmix_losses = 0
    print(f"{'station':8} {'lead':6} {'n':>4}  {'MOSMIX MAE':>11} {'OM-raw MAE':>11} "
          f"{'OM-corr MAE':>12}  {'MOSMIX bias':>12}  {'z mean/std':>12}")
    for (icao, bucket), recs in sorted(per_station_lead.items()):
        mos = mae(recs, "mos_err")
        om_raw = mae(recs, "om_raw_err")
        om_corr = mae(recs, "om_corr_err")
        n = len(recs)
        zs = vals(recs, "mos_z")
        z_str = f"{statistics.mean(zs):+.2f}/{statistics.pstdev(zs):.2f}" if len(zs) >= 2 else "n/a"
        mos_str = f"{mos[0]:.2f} (n={mos[1]})" if mos else "n/a"
        om_raw_str = f"{om_raw[0]:.2f} (n={om_raw[1]})" if om_raw else "n/a"
        om_corr_str = f"{om_corr[0]:.2f} (n={om_corr[1]})" if om_corr else "n/a"
        mos_errs = vals(recs, "mos_err")
        bias_str = f"{statistics.mean(mos_errs):+.2f}" if mos_errs else "n/a"
        print(f"{icao:8} {bucket:6} {n:>4}  {mos_str:>11} {om_raw_str:>11} {om_corr_str:>12}  "
              f"{bias_str:>12}  {z_str:>12}")

        if mos and om_corr and n >= MIN_N_FOR_VERDICT:
            if mos[0] < om_corr[0]:
                mosmix_wins += 1
            elif mos[0] > om_corr[0]:
                mosmix_losses += 1

    print()
    decided = mosmix_wins + mosmix_losses
    if decided == 0:
        print(f"VERDICT: not answerable yet — no (station, lead-bucket) group has reached "
              f"n>={MIN_N_FOR_VERDICT}. Keep scripts/mosmix_snapshot.py running daily and "
              f"re-run this script in 1-3 weeks. Do NOT treat the table above as a verdict.")
    else:
        print(f"MOSMIX beats current pipeline (MAE) in {mosmix_wins}/{decided} "
              f"station/lead groups that reached n>={MIN_N_FOR_VERDICT} "
              f"({100 * mosmix_wins / decided:.0f}%).")
        if mosmix_wins > decided / 2:
            print("-> majority: T1 gate MAE criterion currently MET. Still check the z "
                  "column above for sigma honesty (want std near 1.0, mean near 0) before "
                  "calling this a full go — an MAE win with dishonest sigma is not a pass.")
        else:
            print("-> not a majority: T1 gate MAE criterion currently NOT met. Per the plan: "
                  "if MOSMIX is merely equal or worse, the correct call is to stop, not to "
                  "proceed to T2.")


if __name__ == "__main__":
    main()
