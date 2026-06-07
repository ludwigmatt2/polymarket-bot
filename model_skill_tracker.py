"""
Phase 4 — per-model skill tracker.

Reads resolved paper trades, parses the persisted model_breakdown_json (each model's
P(outcome) at signal time, stored from Phase 0 onward), and scores each model's Brier.
Suggests member weights to replace the labeled literature prior in config.MODEL_WEIGHTS
once each model has enough scored trades.

Weight rule: weight_m = brier_worst / brier_m, so the least-skillful model anchors at
1.0 and better models get proportionally more (matches the prior's shape: ECMWF>ICON>GFS).
Until every model clears MIN_MODEL_OBS the tracker recommends keeping the prior — the
prior is itself empirically grounded (Previous-Runs lead-3 MAE: ECMWF<ICON<GFS).

Usage:
    python model_skill_tracker.py                 # score + suggest
    python model_skill_tracker.py --by-metric     # split by metric
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path

from weather.config import MODEL_WEIGHTS

TRADES_CSV = Path("logs/paper_trades.csv")
MIN_MODEL_OBS = 30   # per-model scored trades before fitted weights are trusted


# ── Pure scoring logic (unit-tested) ────────────────────────────────────────────

def parse_breakdown(raw: str) -> dict[str, float]:
    if not raw:
        return {}
    try:
        d = json.loads(raw)
        return {k: float(v) for k, v in d.items()} if isinstance(d, dict) else {}
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}


def score_models(rows: list[tuple[dict[str, float], int]]) -> dict[str, dict]:
    """rows = [(breakdown, outcome)] → {model: {brier, n}} (Brier of each model's P)."""
    acc: dict[str, list[float]] = defaultdict(list)
    for breakdown, outcome in rows:
        for model, p in breakdown.items():
            acc[model].append((p - outcome) ** 2)
    return {m: {"brier": sum(b) / len(b), "n": len(b)} for m, b in acc.items() if b}


def suggest_weights(scores: dict[str, dict], min_obs: int = MIN_MODEL_OBS) -> dict | None:
    """
    Fitted weights from per-model Brier, or None if any model is under-sampled.
    weight_m = brier_worst / brier_m  (worst model = 1.0, better models > 1.0).
    """
    if not scores or any(s["n"] < min_obs for s in scores.values()):
        return None
    worst = max(s["brier"] for s in scores.values())
    if worst <= 0:
        return None
    return {m: round(worst / s["brier"], 3) for m, s in scores.items()}


# ── Reporting ────────────────────────────────────────────────────────────────────

def _load_rows(by_metric: bool):
    if not TRADES_CSV.exists():
        return {}
    out: dict[str, list] = defaultdict(list)
    for r in csv.DictReader(open(TRADES_CSV)):
        if r.get("actual_outcome") not in ("0", "1"):
            continue
        breakdown = parse_breakdown(r.get("model_breakdown_json", ""))
        if not breakdown:
            continue
        key = r.get("metric", "all") if by_metric else "all"
        out[key].append((breakdown, int(r["actual_outcome"])))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Score per-model skill from resolved trades")
    ap.add_argument("--by-metric", action="store_true")
    args = ap.parse_args()

    groups = _load_rows(args.by_metric)
    print(f"Current prior (config.MODEL_WEIGHTS): {MODEL_WEIGHTS}\n")
    if not groups:
        print("No resolved trades carry model_breakdown_json yet "
              "(it persists from Phase 0 onward). Keep the prior.")
        return

    for key, rows in groups.items():
        scores = score_models(rows)
        print(f"══ {key} ══  ({len(rows)} scored trades)")
        for model in sorted(scores, key=lambda m: scores[m]["brier"]):
            s = scores[model]
            print(f"  {model:<16} brier={s['brier']:.4f}  n={s['n']}")
        fitted = suggest_weights(scores)
        if fitted is None:
            need = MIN_MODEL_OBS
            print(f"  → under-sampled (need ≥{need}/model). Keep the prior.\n")
        else:
            print(f"  → fitted weights: {fitted}")
            print(f"    (to apply: set config.MODEL_WEIGHTS = {fitted})\n")


if __name__ == "__main__":
    main()
