"""Per-station MOSMIX bias table (Phase 3 follow-up to the T1 verdict).

T1 (2026-10-12) found MOSMIX narrowly beats our pipeline on MAE but carries a
real systematic bias at several stations (EGLC, KATL, RJTT, RKSI). A causal
walk-forward (scripts/mosmix_bias_eval.py) showed a shrunk per-(station, kind)
mean-error correction cut MAE ~14% on rows where it was active, 16/16 stations,
robust to k / min-days / lead bucketing.

This module owns the estimator so the offline eval and the live shadow track
cannot drift apart: both call `shrunk_bias()`.

  bias_c = n / (n + k) * mean_over_days(mean error that day)
  error  = mosmix_mean - truth      (positive => MOSMIX runs warm)
  applied: corrected_mean = mosmix_mean - bias_c

Each target DAY counts once (a day's forecasts at several leads are strongly
correlated, so counting rows would overstate n), and only days strictly before
`today` count — the latest truth that is final when a snapshot is taken.
Fewer than `min_days` days -> no entry (the corrector stands down).
"""
from __future__ import annotations

import csv
import json
import statistics
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

DEFAULT_K = 10.0
DEFAULT_MIN_DAYS = 7


def shrunk_bias(day_errors: dict[str, list[float]], k: float = DEFAULT_K,
                min_days: int = DEFAULT_MIN_DAYS) -> tuple[float, int] | None:
    """({target_day: [errors]}) -> (shrunk bias in degC, n_days) or None."""
    day_means = [statistics.mean(v) for v in day_errors.values() if v]
    n = len(day_means)
    if n < min_days:
        return None
    return n / (n + k) * statistics.mean(day_means), n


def fit_table(rows: list[dict], truth: dict[tuple[str, str], dict[str, float]],
              today: date, k: float = DEFAULT_K, min_days: int = DEFAULT_MIN_DAYS) -> dict:
    """rows: archive rows (icao, target_day, kind, mosmix_mean_c). truth maps
    (icao, 'YYYY-MM-DD') -> {'max': degC, 'min': degC}. Returns the table dict."""
    errs: dict[tuple[str, str], dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        if not r.get("mosmix_mean_c") or r["target_day"] >= today.isoformat():
            continue
        t = (truth.get((r["icao"], r["target_day"])) or {}).get(r["kind"])
        if t is None:
            continue
        errs[(r["icao"], r["kind"])][r["target_day"]].append(float(r["mosmix_mean_c"]) - t)
    entries = {}
    for (icao, kind), by_day in sorted(errs.items()):
        est = shrunk_bias(by_day, k, min_days)
        if est is not None:
            entries[f"{icao}|{kind}"] = {"bias_c": round(est[0], 4), "n_days": est[1]}
    return {"generated_at": datetime.now(timezone.utc).isoformat(),
            "k": k, "min_days": min_days, "entries": entries}


class MosmixBiasTable:
    """Read-only lookup over a table written by fit_table. Missing file / bad
    JSON / absent key all degrade to 'no correction' — never raise."""

    def __init__(self, entries: dict | None = None):
        self._entries = entries or {}

    @classmethod
    def load(cls, path: Path) -> "MosmixBiasTable":
        try:
            return cls(json.loads(Path(path).read_text()).get("entries", {}))
        except (OSError, ValueError, AttributeError):
            return cls()

    def lookup(self, icao: str, kind: str) -> float | None:
        e = self._entries.get(f"{icao}|{kind}")
        return float(e["bias_c"]) if e else None

    def __len__(self) -> int:
        return len(self._entries)


def refit_from_archive(archive_path: Path, out_path: Path, today: date | None = None,
                       k: float = DEFAULT_K, min_days: int = DEFAULT_MIN_DAYS) -> int:
    """Rebuild the bias table from the MOSMIX archive + IEM truth and write it
    atomically. One IEM range call per station. Returns the entry count."""
    from . import iem_client

    today = today or datetime.now(timezone.utc).date()
    if not Path(archive_path).exists():
        return 0
    with open(archive_path) as f:
        rows = list(csv.DictReader(f))
    by_icao: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        if r.get("mosmix_mean_c") and r["target_day"] < today.isoformat():
            by_icao[r["icao"]].append(r["target_day"])
    truth: dict[tuple[str, str], dict[str, float]] = {}
    for icao, days in by_icao.items():
        rng = iem_client.daily_range(icao, date.fromisoformat(min(days)), date.fromisoformat(max(days)))
        for d, v in rng.items():
            t = {kind: iem_client.f_to_c(v[f"{kind}_f"]) for kind in ("max", "min")
                 if v.get(f"{kind}_f") is not None}
            if t:
                truth[(icao, d)] = t
    table = fit_table(rows, truth, today, k, min_days)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(table, indent=1))
    tmp.replace(out_path)
    return len(table["entries"])
