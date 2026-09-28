"""MOSMIX forward archiver — Phase 3 T1 (docs/PHASE3_STATION_FORECAST_PLAN.md).

DWD's MOSMIX open-data endpoint keeps NO historical archive — verified
2026-09-28 by listing a station's directory: only ~2-3 days of past runs are
retained (8 files spanning 2026-09-26T15Z -> 2026-09-28T09Z, then gone), and
DWD's separate CDC climate archive holds observations/reanalysis, not MOSMIX
point forecasts. Open-Meteo's own ensemble API is the same story (~3-4 days,
see ecmwf_archive_client.py's docstring). Neither can be backtested
retroactively — there is no way to ask "what would MOSMIX have forecast a
month ago" after the fact.

So T1's "does MOSMIX beat our pipeline at the station" question is answered
by snapshotting all three forecasts — MOSMIX, the raw Open-Meteo ensemble
mean, and our skill-corrected mean — for the SAME station/day/moment, and
letting scripts/mosmix_backcheck.py join them against IEM truth once each
target day resolves. This sidesteps the archive-depth problem entirely: we
stop trying to reconstruct history and start recording it ourselves, live.

One row proves nothing. Run this daily (deploy/systemd/pmbot-mosmix-snapshot.*)
and give it 2-3 weeks before trusting mosmix_backcheck.py's read — the same
"no decision under ~250" discipline edge_decay_sep2026.md established for
trade edge, applied here to forecast MAE.

Run: venv/bin/python scripts/mosmix_snapshot.py [--icao KLGA KATL ...] [--lead-days 6]
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from weather import iem_client, mosmix_client  # noqa: E402
from weather.models import Location  # noqa: E402
from weather.paths import DATA_DIR  # noqa: E402
from weather.probability_model import HistoricalSkillCorrector  # noqa: E402
from weather.weather_client import WeatherClient  # noqa: E402

# 0..6 — near-term horizon (most traded markets resolve within a week; MOSMIX's
# own horizon runs to +9 but the far end matters least for a go/no-go read).
DEFAULT_LEAD_DAYS = 7
KINDS = {"max": "temperature_2m_max", "min": "temperature_2m_min"}
ARCHIVE_PATH = DATA_DIR / "logs" / "mosmix_archive.csv"
FIELDS = [
    "icao", "wmo", "snapshot_at", "target_day", "kind", "lead_days",
    "mosmix_mean_c", "mosmix_sigma_c", "om_raw_mean_c", "om_corrected_mean_c", "om_n_members",
]


def _existing_keys(path: Path) -> set[tuple[str, str, str, str]]:
    """(icao, target_day, kind, snapshot-date) already on disk — the dedup key.
    Keying on snapshot DATE (not full timestamp) makes a same-day rerun a no-op,
    so an accidental double-fire of the timer can't duplicate rows."""
    if not path.exists():
        return set()
    keys = set()
    with path.open() as f:
        for row in csv.DictReader(f):
            keys.add((row["icao"], row["target_day"], row["kind"], row["snapshot_at"][:10]))
    return keys


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--icao", nargs="+", default=None, help="Limit to these ICAOs (default: full registry)")
    ap.add_argument("--lead-days", type=int, default=DEFAULT_LEAD_DAYS, help="Forward days to snapshot (default 7)")
    args = ap.parse_args()

    icaos = args.icao or list(iem_client._STATION_REGISTRY)

    skill = HistoricalSkillCorrector()
    client = WeatherClient()
    now = datetime.now(timezone.utc)
    snapshot_at = now.isoformat()
    today_key = now.date().isoformat()
    existing = _existing_keys(ARCHIVE_PATH)

    ARCHIVE_PATH.parent.mkdir(parents=True, exist_ok=True)
    is_new = not ARCHIVE_PATH.exists()
    written = skipped_dup = skipped_nodata = skipped_no_wmo = 0

    with ARCHIVE_PATH.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if is_new:
            w.writeheader()

        for icao in icaos:
            meta = iem_client.station_meta(icao)
            if not meta:
                continue
            wmo = meta["wmo"]
            if not wmo:
                skipped_no_wmo += 1
                continue

            fc = mosmix_client.fetch_station(wmo)
            tz = ZoneInfo(meta["tz"])
            today_local = now.astimezone(tz).date()
            loc = Location(city=icao, lat=meta["lat"], lon=meta["lon"], timezone=meta["tz"])

            for lead in range(args.lead_days):
                day = today_local + timedelta(days=lead)

                for kind, metric in KINDS.items():
                    key = (icao, day.isoformat(), kind, today_key)
                    if key in existing:
                        skipped_dup += 1
                        continue

                    mos = mosmix_client.daily_extreme(fc, day, meta["tz"], kind) if fc else None

                    try:
                        ens = client.get_ensemble_forecast(loc, day, metric)
                    except Exception:  # noqa: BLE001 — one bad combo must not sink the whole run
                        ens = None
                    members = ens.all_members if ens else []
                    om_raw = (sum(members) / len(members)) if members else None

                    om_corr = om_raw
                    if om_raw is not None:
                        shift = skill.lookup_shift(meta["lat"], meta["lon"], metric, lead, day.month)
                        if shift is not None:
                            om_corr = om_raw - shift

                    if mos is None and om_raw is None:
                        skipped_nodata += 1
                        continue

                    w.writerow({
                        "icao": icao, "wmo": wmo, "snapshot_at": snapshot_at,
                        "target_day": day.isoformat(), "kind": kind, "lead_days": lead,
                        "mosmix_mean_c": mos[0] if mos else "",
                        "mosmix_sigma_c": mos[1] if mos else "",
                        "om_raw_mean_c": om_raw if om_raw is not None else "",
                        "om_corrected_mean_c": om_corr if om_corr is not None else "",
                        "om_n_members": len(members),
                    })
                    written += 1

    print(f"mosmix_snapshot: wrote {written} rows, skipped {skipped_dup} dup / "
          f"{skipped_nodata} no-data / {skipped_no_wmo} no-wmo -> {ARCHIVE_PATH}")


if __name__ == "__main__":
    main()
