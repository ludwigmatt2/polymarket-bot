"""
Replay backtest harness (Phase 0.5 of WEATHER_MODEL_UPGRADE.md).

The measuring stick: replay every resolved paper trade through the model under a
chosen *config* and score the recomputed probability against the real outcome.
Used to gate every later phase — a change ships only if it improves Brier on the
affected direction split, not just in aggregate.

Two replay modes (a config picks one via its `predict` function):
  - exact   : reuse the persisted raw_p / model_breakdown_json (Phase-0+ rows only).
              Fast, no network. Exact for post-raw_p changes (calibration, shrinkage,
              bias, weighting). Currently few rows qualify (log predates Phase 0).
  - refetch : re-pull the archive ensemble members for (lat, lon, date, metric),
              cache them, and recompute raw_p. Needed for raw_p-touching changes
              (MOS member-shift, KDE bandwidth).
              ⚠ MEASURED LIMITATION (2026-06): the free Open-Meteo ensemble API only
              retains ~4 days of member history (verified: dates ≥5 days old return
              zero members via both start_date/end_date and past_days). So refetch
              covers only the most recent handful of trades — enough for a directional
              smoke test, NOT for statistically meaningful per-direction Brier deltas.
              The exact-replay path is the real measuring stick and grows as Phase-0
              rows (which persist raw_p) accumulate.

A `config` is just a name + a `predict(members_by_model, trade) -> p` callable.
`baseline_config()` reproduces current production. Later phases add candidates and
compare with `compare(baseline, candidate)`.

CLI:
    python replay_backtest.py --config baseline [--limit N] [--no-cache]
    python replay_backtest.py --compare baseline:candidate   (later phases register these)
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Callable

from weather.city_bias import CityBiasCorrector
from weather.config import LEAD_TIME_DECAY_PER_DAY, MAX_ENSEMBLE_SPREAD, MODEL_WEIGHTS
from weather.models import Location
from weather.probability_model import _apply_kde, _fraction_satisfying
from weather.weather_client import WeatherClient

PAPER_TRADES_LOG = Path("logs/paper_trades.csv")
CACHE_DIR = Path("logs/backtest_cache")

PredictFn = Callable[[dict[str, list[float]], dict], float | None]


# ── Config ─────────────────────────────────────────────────────────────────────

@dataclass
class BacktestConfig:
    name: str
    predict: PredictFn
    needs_members: bool = True   # False ⇒ exact mode (uses persisted raw_p only)


# ── Production-faithful pipeline pieces (shared by candidates) ──────────────────

_PROD_BIAS = CityBiasCorrector()


def _model_breakdown(members_by_model: dict[str, list[float]], threshold, direction, threshold_high) -> dict[str, float]:
    return {
        m: _fraction_satisfying(vals, threshold, direction, threshold_high)
        for m, vals in members_by_model.items() if vals
    }


def _spread(breakdown: dict[str, float]) -> float:
    if len(breakdown) < 2:
        return 0.0
    probs = list(breakdown.values())
    mean_p = sum(probs) / len(probs)
    return math.sqrt(sum((p - mean_p) ** 2 for p in probs) / len(probs))


def _raw_p(members: list[float], threshold, direction, threshold_high, weights=None) -> float:
    raw_p = _fraction_satisfying(members, threshold, direction, threshold_high, weights)
    if len(members) >= 10:
        raw_p = _apply_kde(members, threshold, direction, threshold_high, raw_p, weights)
    return raw_p


def _days_to_res(trade: dict) -> float:
    sig = datetime.fromisoformat(trade["signal_time"])
    res = datetime.fromisoformat(trade["resolution_date"])
    return (res - sig).total_seconds() / 86400.0


def _shrink(p: float, days_to_res: float, spread: float) -> float:
    skill = max(0.5, 1.0 - LEAD_TIME_DECAY_PER_DAY * max(0.0, days_to_res - 1.0))
    spread_factor = max(0.0, 1.0 - spread / MAX_ENSEMBLE_SPREAD)
    return 0.5 + (p - 0.5) * skill * spread_factor


def _thresholds(trade: dict) -> tuple[float, float | None]:
    threshold = float(trade["threshold"])
    threshold_high = float(trade["threshold_high"]) if trade.get("threshold_high") else None
    return threshold, threshold_high


def production_predict(
    members_by_model: dict[str, list[float]],
    trade: dict,
    *,
    bias_offset: float = 0.0,
    weights: dict[str, float] | None = None,
    member_shift: float = 0.0,
) -> float | None:
    """
    Reproduce SignalGenerator + ProbabilityModel for one trade from refetched members.

    Knobs let candidates diverge from baseline along one axis at a time:
      bias_offset  : °C subtracted from the threshold (Phase 2/3 city bias).
      weights      : per-model weights for a weighted fraction (Phase 4).
      member_shift : °C subtracted from every member before counting (Phase 1 MOS).

    No calibrator is applied — the production calibrator was reset to clean state in
    Phase 0, so current production runs uncalibrated. Returns None if no members.
    """
    flat = [v for vals in members_by_model.values() for v in vals]
    if not flat:
        return None
    direction = trade["weather_direction"]
    threshold, threshold_high = _thresholds(trade)
    threshold -= bias_offset
    if threshold_high is not None:
        threshold_high -= bias_offset

    if member_shift:
        members_by_model = {m: [v - member_shift for v in vals] for m, vals in members_by_model.items()}

    breakdown = _model_breakdown(members_by_model, threshold, direction, threshold_high)

    # Pool members and (Phase 4) build a parallel per-member weight vector, mirroring
    # ProbabilityModel.compute_probability — member-level weighted fraction + weighted KDE.
    flat, member_w = [], []
    for model, vals in members_by_model.items():
        w = weights.get(model, 1.0) if weights else 1.0
        flat.extend(vals)
        member_w.extend([w] * len(vals))
    raw_p = _raw_p(flat, threshold, direction, threshold_high, member_w if weights else None)

    return _shrink(raw_p, _days_to_res(trade), _spread(breakdown))


def baseline_config() -> BacktestConfig:
    """Current production: production CityBiasCorrector (reliable-only, no month), no weights."""
    def predict(members_by_model, trade):
        offset = _PROD_BIAS.get_offset(float(trade["lat"]), float(trade["lon"]))
        return production_predict(members_by_model, trade, bias_offset=offset)
    return BacktestConfig("baseline", predict)


# ── Member refetch + cache ──────────────────────────────────────────────────────

def _cache_key(trade: dict) -> str:
    res = datetime.fromisoformat(trade["resolution_date"]).date().isoformat()
    return f"{float(trade['lat']):.4f}_{float(trade['lon']):.4f}_{res}_{trade['metric']}"


def fetch_members(client: WeatherClient, trade: dict, use_cache: bool = True) -> dict[str, list[float]] | None:
    """Refetch (and cache) the archive ensemble members for a resolved trade."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = CACHE_DIR / f"{_cache_key(trade)}.json"
    if use_cache and cache_file.exists():
        try:
            data = json.loads(cache_file.read_text())
            return data or None
        except Exception:
            pass

    loc = Location(city="", lat=float(trade["lat"]), lon=float(trade["lon"]),
                   timezone=trade.get("location_tz") or "UTC")
    res_date = datetime.fromisoformat(trade["resolution_date"]).date()
    forecast = client.get_historical_ensemble_forecast(loc, res_date, trade["metric"])
    members = forecast.member_arrays
    cache_file.write_text(json.dumps(members))
    return members or None


# ── Scoring ─────────────────────────────────────────────────────────────────────

@dataclass
class Split:
    n: int = 0
    brier_sum: float = 0.0
    correct: int = 0
    p_sum: float = 0.0
    preds: list[tuple[float, int]] = field(default_factory=list)  # (p, outcome)

    def add(self, p: float, outcome: int) -> None:
        self.n += 1
        self.brier_sum += (p - outcome) ** 2
        self.correct += int(round(p) == outcome)
        self.p_sum += p
        self.preds.append((p, outcome))

    @property
    def brier(self) -> float:
        return self.brier_sum / self.n if self.n else float("nan")

    @property
    def accuracy(self) -> float:
        return self.correct / self.n if self.n else float("nan")

    @property
    def mean_p(self) -> float:
        return self.p_sum / self.n if self.n else float("nan")


@dataclass
class BacktestReport:
    config_name: str
    overall: Split
    by_direction: dict[str, Split]
    by_side: dict[str, Split]
    n_total: int
    n_scored: int

    def coverage(self) -> float:
        return self.n_scored / self.n_total if self.n_total else 0.0


def load_resolved_trades(path: Path = PAPER_TRADES_LOG) -> list[dict]:
    if not path.exists():
        return []
    rows = list(csv.DictReader(open(path)))
    return [r for r in rows if r.get("actual_outcome") in ("0", "1")]


def run_backtest(
    config: BacktestConfig,
    trades: list[dict],
    client: WeatherClient | None = None,
    use_cache: bool = True,
    members_provider: Callable[[dict], dict | None] | None = None,
) -> BacktestReport:
    """
    Score `config` over `trades`. `members_provider` overrides member fetching
    (used by tests to inject members without network).
    """
    if config.needs_members and members_provider is None:
        client = client or WeatherClient()
        members_provider = lambda t: fetch_members(client, t, use_cache=use_cache)

    overall = Split()
    by_direction: dict[str, Split] = defaultdict(Split)
    by_side: dict[str, Split] = defaultdict(Split)
    n_scored = 0

    for t in trades:
        outcome = int(t["actual_outcome"])
        members = members_provider(t) if config.needs_members else {}
        if config.needs_members and not members:
            continue
        p = config.predict(members or {}, t)
        if p is None:
            continue
        n_scored += 1
        overall.add(p, outcome)
        by_direction[t["weather_direction"]].add(p, outcome)
        by_side[t["direction"]].add(p, outcome)

    return BacktestReport(
        config_name=config.name,
        overall=overall,
        by_direction=dict(by_direction),
        by_side=dict(by_side),
        n_total=len(trades),
        n_scored=n_scored,
    )


# ── Reporting ────────────────────────────────────────────────────────────────────

def _fmt_split(label: str, s: Split, base: Split | None = None) -> str:
    if s.n == 0:
        return f"  {label:<10} n=0"
    delta = ""
    if base is not None and base.n:
        d = s.brier - base.brier
        delta = f"   Δbrier={d:+.4f}"
    return (f"  {label:<10} n={s.n:<4} brier={s.brier:.4f} "
            f"acc={s.accuracy:.3f} meanP={s.mean_p:.3f}{delta}")


def print_report(report: BacktestReport, baseline: BacktestReport | None = None) -> None:
    print(f"\n══════════ Backtest: {report.config_name} ══════════")
    print(f"  scored {report.n_scored}/{report.n_total} resolved trades "
          f"(coverage {report.coverage():.1%})")
    base_overall = baseline.overall if baseline else None
    print("\n  OVERALL")
    print(_fmt_split("all", report.overall, base_overall))
    print("\n  BY DIRECTION")
    for d in ("equal", "range", "above", "below"):
        if d in report.by_direction:
            base = baseline.by_direction.get(d) if baseline else None
            print(_fmt_split(d, report.by_direction[d], base))
    print("\n  BY SIDE (bet direction)")
    for side in ("YES", "NO"):
        if side in report.by_side:
            base = baseline.by_side.get(side) if baseline else None
            print(_fmt_split(side, report.by_side[side], base))
    print("════════════════════════════════════════════════\n")


def mos_config() -> BacktestConfig:
    """Phase 1 MOS: member-shift by historical mean_error; city bias stands down where MOS covers."""
    from weather.probability_model import HistoricalSkillCorrector
    mos = HistoricalSkillCorrector()

    def predict(members_by_model, trade):
        lat, lon, metric = float(trade["lat"]), float(trade["lon"]), trade["metric"]
        res = datetime.fromisoformat(trade["resolution_date"])
        sig = datetime.fromisoformat(trade["signal_time"])
        lead = max(1, round((res - sig).total_seconds() / 86400))
        shift = mos.lookup_shift(lat, lon, metric, lead, res.month) or 0.0
        bias = 0.0 if mos.covers(metric) else _PROD_BIAS.get_offset(lat, lon)
        return production_predict(members_by_model, trade, bias_offset=bias, member_shift=shift)
    return BacktestConfig("mos", predict)


def nobias_config() -> BacktestConfig:
    """Ablation: no city-bias correction at all (offset 0)."""
    def predict(members_by_model, trade):
        return production_predict(members_by_model, trade, bias_offset=0.0)
    return BacktestConfig("nobias", predict)


def weighted_config() -> BacktestConfig:
    """Phase 4 in isolation: no bias, no MOS — just the per-model weight prior."""
    def predict(members_by_model, trade):
        return production_predict(members_by_model, trade, weights=MODEL_WEIGHTS)
    return BacktestConfig("weighted", predict)


def mos_weighted_config() -> BacktestConfig:
    """Shipped reality: MOS member-shift + per-model weighting (marginal weighting on top of MOS)."""
    from weather.probability_model import HistoricalSkillCorrector
    mos = HistoricalSkillCorrector()

    def predict(members_by_model, trade):
        lat, lon, metric = float(trade["lat"]), float(trade["lon"]), trade["metric"]
        res = datetime.fromisoformat(trade["resolution_date"])
        sig = datetime.fromisoformat(trade["signal_time"])
        lead = max(1, round((res - sig).total_seconds() / 86400))
        shift = mos.lookup_shift(lat, lon, metric, lead, res.month) or 0.0
        bias = 0.0 if mos.covers(metric) else _PROD_BIAS.get_offset(lat, lon)
        return production_predict(members_by_model, trade, bias_offset=bias,
                                  member_shift=shift, weights=MODEL_WEIGHTS)
    return BacktestConfig("mos_weighted", predict)


# Registry of named configs so the CLI / later phases can address them by name.
# `baseline` reflects whatever the production CityBiasCorrector currently does, so
# to A/B a bias change, compare against `nobias` (the ablation) rather than baseline.
CONFIG_REGISTRY: dict[str, Callable[[], BacktestConfig]] = {
    "baseline": baseline_config,
    "nobias": nobias_config,
    "mos": mos_config,
    "weighted": weighted_config,
    "mos_weighted": mos_weighted_config,
}


def _main() -> None:
    ap = argparse.ArgumentParser(description="Replay backtest over resolved paper trades")
    ap.add_argument("--config", default="baseline", help="config name from CONFIG_REGISTRY")
    ap.add_argument("--compare", default="", help="baseline:candidate — print Δ")
    ap.add_argument("--limit", type=int, default=0, help="only the last N resolved trades (0=all)")
    ap.add_argument("--no-cache", action="store_true", help="ignore the member cache")
    args = ap.parse_args()

    trades = load_resolved_trades()
    if args.limit:
        trades = trades[-args.limit:]
    client = WeatherClient()
    use_cache = not args.no_cache

    if args.compare:
        base_name, cand_name = args.compare.split(":")
        base = run_backtest(CONFIG_REGISTRY[base_name](), trades, client, use_cache)
        cand = run_backtest(CONFIG_REGISTRY[cand_name](), trades, client, use_cache)
        print_report(base)
        print_report(cand, baseline=base)
    else:
        report = run_backtest(CONFIG_REGISTRY[args.config](), trades, client, use_cache)
        print_report(report)


if __name__ == "__main__":
    _main()
