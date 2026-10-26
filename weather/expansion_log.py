"""Calibration log for ALL evaluated station markets (station expansion, Oct 2026).

Only ~4 of ~1,500 evaluated markets per scan pass the gates, so trades alone would
take weeks to say anything about a new station. This logs (model_p, market_p) for
every evaluated market — one row per market per UTC day — and later labels them with
the on-chain outcome (preferred) or the station reading, so per-station Brier skill
and bias are measurable without trading. Rows carry expansion=1/0 so the original
14 stations act as the control. Read-only side channel: never gates anything.
"""
from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ._io import atomic_write_csv
from .config import EXPANSION_STATIONS
from .paper_trader import fc_stats

LOG_NAME = "expansion_calibration.csv"
FIELDS = [
    "logged_at", "market_id", "station_icao", "expansion", "metric", "threshold",
    "threshold_high", "weather_direction", "resolve_unit", "station_country",
    "location_tz", "resolution_date", "lead_h", "model_p", "raw_p", "market_p",
    "rejection", "actual_outcome", "label_source", "resolved_at",
    "fc_mean", "fc_member_std",
]
# Event-day end + settle buffer before a station reading is trusted.
_SETTLE_H = 30


def log_scan(signals, path: Path, now: datetime | None = None) -> int:
    """Append one row per (market, UTC day) for every station-resolved signal."""
    now = now or datetime.now(timezone.utc)
    day = now.date().isoformat()
    seen: set[tuple[str, str]] = set()
    if path.exists():
        with open(path, newline="") as f:
            header = next(csv.reader(f), [])
        if header != FIELDS:  # columns added since the file was created
            with open(path, newline="") as f:
                old = list(csv.DictReader(f))
            atomic_write_csv(path, FIELDS, old)
        with open(path, newline="") as f:
            seen = {(r["market_id"], r["logged_at"][:10]) for r in csv.DictReader(f)}
    rows = []
    for s in signals:
        m = s.market
        if not m.station_icao or (m.market_id, day) in seen:
            continue
        seen.add((m.market_id, day))
        res = m.resolution_date if m.resolution_date.tzinfo else m.resolution_date.replace(tzinfo=timezone.utc)
        rows.append({
            "logged_at": now.isoformat(), "market_id": m.market_id,
            "station_icao": m.station_icao,
            "expansion": int(m.station_icao.upper() in EXPANSION_STATIONS),
            "metric": m.metric, "threshold": m.threshold,
            "threshold_high": "" if m.threshold_high is None else m.threshold_high,
            "weather_direction": m.direction, "resolve_unit": m.resolve_unit,
            "station_country": m.station_country, "location_tz": m.location.timezone or "UTC",
            "resolution_date": res.isoformat(),
            "lead_h": round((res - now).total_seconds() / 3600, 1),
            "model_p": round(float(s.model_p), 4),
            "raw_p": round(float(getattr(s.prob_result, "raw_p", s.model_p)), 4),
            "market_p": round(float(s.market_p), 4),
            "rejection": (s.rejection_reason or "").split(":")[0],
            "actual_outcome": "", "label_source": "", "resolved_at": "",
            "fc_mean": "" if fc_stats(s)[0] is None else fc_stats(s)[0],
            "fc_member_std": "" if fc_stats(s)[1] is None else fc_stats(s)[1],
        })
    if not rows:
        return 0
    new = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows(rows)
    return len(rows)


def resolve_rows(path: Path, now: datetime | None = None, max_rows: int = 3000) -> int:
    """Label unresolved rows: on-chain payout first, else the station reading."""
    from .paper_trader import _local_event_date
    from .relayer import chain_outcome
    from .station_truth import station_outcome

    if not path.exists():
        return 0
    now = now or datetime.now(timezone.utc)
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    cache: dict = {}
    chain_cache: dict[str, bool | None] = {}
    done = 0
    for r in rows:
        if r["actual_outcome"] != "" or done >= max_rows:
            continue
        res = datetime.fromisoformat(r["resolution_date"])
        if now < res + timedelta(hours=_SETTLE_H):
            continue
        mid = r["market_id"]
        if mid not in chain_cache:
            try:
                chain_cache[mid] = chain_outcome(mid)
            except Exception:  # noqa: BLE001 — RPC hiccup: fall through to station
                chain_cache[mid] = None
        out, src = chain_cache[mid], "onchain"
        if out is None:
            th = float(r["threshold"])
            hi = float(r["threshold_high"]) if r["threshold_high"] != "" else None
            try:
                out, _s, _v = station_outcome(
                    r["station_icao"], r["station_country"], r["resolve_unit"],
                    _local_event_date(res, r["location_tz"]), r["metric"], th, hi,
                    r["weather_direction"], cache=cache)
            except Exception:  # noqa: BLE001
                out = None
            src = "station"
        if out is None:
            continue
        r["actual_outcome"], r["label_source"] = int(out), src
        r["resolved_at"] = now.isoformat()
        done += 1
    if done:
        atomic_write_csv(path, FIELDS, rows)
    return done


def station_report(path: Path) -> list[dict]:
    """Per-station n / Brier(model) / Brier(market) / mean(model_p - outcome)."""
    if not path.exists():
        return []
    by: dict[str, list[dict]] = {}
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            if r["actual_outcome"] in ("0", "1"):
                by.setdefault(r["station_icao"], []).append(r)
    out = []
    for icao, rs in by.items():
        y = [int(r["actual_outcome"]) for r in rs]
        mp = [float(r["model_p"]) for r in rs]
        kp = [float(r["market_p"]) for r in rs]
        n = len(rs)
        out.append({
            "station": icao, "expansion": rs[0]["expansion"], "n": n,
            "brier_model": sum((p - o) ** 2 for p, o in zip(mp, y)) / n,
            "brier_market": sum((p - o) ** 2 for p, o in zip(kp, y)) / n,
            "bias": sum(p - o for p, o in zip(mp, y)) / n,
            "onchain_frac": sum(r["label_source"] == "onchain" for r in rs) / n,
        })
    return sorted(out, key=lambda d: (d["expansion"], d["station"]))
