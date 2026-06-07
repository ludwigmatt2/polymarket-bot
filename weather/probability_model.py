"""
Ensemble → calibrated P(outcome) model.

Core logic:
1. Raw probability: fraction of ensemble members satisfying condition
2. Gaussian KDE smoothing to reduce discretization artifacts
3. Calibration once MIN_CALIBRATION_OBS resolved observations are collected:
   - Platt scaling (logistic) below PLATT_THRESHOLD obs — stable at small N
   - Isotonic regression above PLATT_THRESHOLD obs — more flexible at large N
   - Per-direction calibrators (equal/range/above/below) when enough data per type
4. Uncertainty gate: reject signals when cross-model spread is too high
"""

from __future__ import annotations

import csv
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

import numpy as np
from scipy.stats import gaussian_kde

from .city_bias import _haversine
from .config import (
    HISTORICAL_SKILL_PATH,
    MAX_ENSEMBLE_SPREAD,
    MIN_ENSEMBLE_MEMBERS,
    MIN_SKILL_OBS,
    MODEL_WEIGHTING_ENABLED,
    MODEL_WEIGHTS,
    MOS_ENABLED,
    MOS_METRICS,
)
from .models import EnsembleForecast, RawProbabilityResult


class HistoricalSkillCorrector:
    """
    Phase 1 non-parametric MOS. Loads logs/historical_skill.json (built by
    build_historical_skill.py from the Previous-Runs forecast-error record) and
    shifts ensemble members by −mean_error for the matching (city, lead, month),
    removing the model's systematic warm/cold bias before raw_p is counted.

    Only `enabled_metrics` are corrected (validated to help; precipitation excluded).
    Cells below MIN_SKILL_OBS fall back: (lead,month) → (lead, all-months) →
    (nearest lead, …). Returns members unchanged when no trustworthy cell exists.
    """

    def __init__(
        self,
        path: Path = Path(HISTORICAL_SKILL_PATH),
        enabled_metrics: frozenset[str] = MOS_METRICS,
        max_distance_km: float = 100.0,
    ):
        self.enabled_metrics = enabled_metrics
        self.max_distance_km = max_distance_km
        self._cities: list[dict] = []
        self.load_error: str | None = None
        if path.exists():
            try:
                table = json.loads(path.read_text())
                self._cities = list(table.values())
            except Exception as exc:
                self.load_error = str(exc)
                _log.warning("Historical skill load failed — MOS disabled: %s", exc)

    @property
    def is_loaded(self) -> bool:
        return bool(self._cities)

    def covers(self, metric: str) -> bool:
        """True if MOS owns correction for this metric (so flat city bias must stand down)."""
        return MOS_ENABLED and self.is_loaded and metric in self.enabled_metrics

    def _nearest_city(self, lat: float, lon: float) -> dict | None:
        best, best_d = None, float("inf")
        for c in self._cities:
            d = _haversine(lat, lon, c["lat"], c["lon"])
            if d < best_d:
                best_d, best = d, c
        return best if best and best_d < self.max_distance_km else None

    def lookup_shift(self, lat: float, lon: float, metric: str, lead_day: int, month: int) -> float | None:
        """Return the °C member shift (mean_error) for this cell, or None if untrusted."""
        if not MOS_ENABLED or metric not in self.enabled_metrics:
            return None
        city = self._nearest_city(lat, lon)
        if not city:
            return None
        ms = city.get("metrics", {}).get(metric)
        if not ms:
            return None
        lead = max(1, min(int(round(lead_day)), 5))
        # priority: exact (lead,month) → (lead, all-months) → nearest lead (month) → nearest lead (all-months)
        ordered_leads = sorted(ms.keys(), key=lambda L: abs(int(L) - lead))
        candidates = [ms.get(str(lead), {}).get(str(month)),
                      ms.get(str(lead), {}).get("0")]
        for L in ordered_leads:
            candidates.append(ms[L].get(str(month)))
            candidates.append(ms[L].get("0"))
        for cell in candidates:
            if cell and cell.get("n", 0) >= MIN_SKILL_OBS:
                return cell["mean_error"]
        return None

    def adjust_members(
        self, members: list[float], lat: float, lon: float, metric: str, lead_day: int, month: int
    ) -> list[float]:
        """Shift members DOWN by mean_error (forecast−actual); warm bias ⇒ shift down."""
        shift = self.lookup_shift(lat, lon, metric, lead_day, month)
        if shift is None:
            return members
        return [m - shift for m in members]


class ProbabilityModel:
    MIN_CALIBRATION_OBS = 30    # minimum to activate any calibrator
    PLATT_THRESHOLD = 300       # switch from Platt to isotonic above this
    REFIT_INTERVAL = 10         # refit calibrators every N new observations

    def __init__(
        self,
        calibration_log_path: Path = Path("logs/calibration_log.csv"),
        skill_corrector: "HistoricalSkillCorrector | None" = None,
        model_weights: dict[str, float] | None = None,
    ):
        self.calibration_log_path = calibration_log_path
        # Phase 4: per-model member weights. None → the labeled literature prior.
        self.model_weights = MODEL_WEIGHTS if model_weights is None else model_weights
        # Phase 1 MOS. Auto-load by default: a no-op when historical_skill.json is
        # absent or the metric isn't covered, and only active when callers pass
        # lead_day/month (the live signal path does; most unit tests don't).
        if skill_corrector is None:
            skill_corrector = HistoricalSkillCorrector()
        self.skill_corrector = skill_corrector
        self._calibrator: Any = None                               # global calibrator
        self._calibrators_by_dir: dict[str, Any] = {}             # per-direction
        self._calibration_obs: list[tuple[float, float]] = []     # (model_p, actual) global
        self._calibration_obs_by_dir: dict[str, list[tuple[float, float]]] = {}
        self.calibration_load_error: str | None = None
        self._load_calibration_data()

    def compute_probability(
        self,
        forecast: EnsembleForecast,
        threshold: float,
        direction: str,
        threshold_high: float | None = None,
        lead_day: int | None = None,
        month: int | None = None,
    ) -> RawProbabilityResult:
        """Full pipeline: (MOS member-shift) → raw P → KDE smoothing → calibration."""
        member_arrays = forecast.member_arrays
        # Phase 1 MOS: shift each model's members by −mean_error before counting,
        # so raw_p itself is bias-corrected. Applied per-model so the breakdown and
        # the pooled fraction stay consistent. Needs lead/month from the caller.
        if (
            self.skill_corrector is not None
            and lead_day is not None
            and month is not None
            and self.skill_corrector.covers(forecast.metric)
        ):
            shift = self.skill_corrector.lookup_shift(
                forecast.lat, forecast.lon, forecast.metric, lead_day, month
            )
            if shift is not None:
                member_arrays = {m: [v - shift for v in vals] for m, vals in member_arrays.items()}

        # Phase 4: pool members and build a parallel per-member weight vector so the
        # more skillful model (ECMWF) gets amplified beyond its raw member count.
        # Equal weights reproduce the pre-Phase-4 member-pooled behavior exactly.
        members: list[float] = []
        member_weights: list[float] = []
        for model, vals in member_arrays.items():
            w = self.model_weights.get(model, 1.0) if MODEL_WEIGHTING_ENABLED else 1.0
            members.extend(vals)
            member_weights.extend([w] * len(vals))

        if not members:
            return RawProbabilityResult(
                raw_p=0.5, calibrated_p=0.5,
                ensemble_spread=1.0, n_members=0,
                is_calibrated=False,
                model_breakdown={}, threshold=threshold,
                direction=direction, metric=forecast.metric,
            )

        model_breakdown: dict[str, float] = {}
        for model, model_members in member_arrays.items():
            if model_members:
                model_breakdown[model] = _fraction_satisfying(
                    model_members, threshold, direction, threshold_high
                )

        raw_p = _fraction_satisfying(members, threshold, direction, threshold_high, member_weights)

        if len(members) >= 10:
            raw_p = _apply_kde(members, threshold, direction, threshold_high, raw_p, member_weights)

        calibrated_p = self._apply_calibration(raw_p, direction)

        if len(model_breakdown) >= 2:
            probs = list(model_breakdown.values())
            mean_p = sum(probs) / len(probs)
            spread = float(np.sqrt(sum((p - mean_p) ** 2 for p in probs) / len(probs)))
        else:
            spread = 0.0

        return RawProbabilityResult(
            raw_p=raw_p,
            calibrated_p=calibrated_p,
            ensemble_spread=spread,
            n_members=len(members),
            is_calibrated=self._calibrator is not None or direction in self._calibrators_by_dir,
            model_breakdown=model_breakdown,
            threshold=threshold,
            direction=direction,
            metric=forecast.metric,
            n_models=len(model_breakdown),
        )

    @property
    def n_calibration_obs(self) -> int:
        return len(self._calibration_obs)

    def is_confident(self, result: RawProbabilityResult) -> bool:
        if result.n_members < MIN_ENSEMBLE_MEMBERS:
            return False
        if result.ensemble_spread > MAX_ENSEMBLE_SPREAD:
            return False
        return True

    def log_observation(self, model_p: float, actual_outcome: bool, direction: str = "") -> None:
        """
        Record a resolved observation for calibration.
        Appends to the CSV and refits calibrators when thresholds are reached.
        """
        obs = (model_p, float(actual_outcome))
        self._calibration_obs.append(obs)
        if direction:
            self._calibration_obs_by_dir.setdefault(direction, []).append(obs)
        self._append_calibration_csv(model_p, actual_outcome, direction)
        n = len(self._calibration_obs)
        if n >= self.MIN_CALIBRATION_OBS and n % self.REFIT_INTERVAL == 0:
            self._fit_calibrator()

    def _apply_calibration(self, raw_p: float, direction: str = "") -> float:
        # Direction-specific calibrator takes priority
        if direction and direction in self._calibrators_by_dir:
            return _predict(self._calibrators_by_dir[direction], raw_p)
        if self._calibrator is not None:
            return _predict(self._calibrator, raw_p)
        return raw_p

    def _fit_calibrator(self) -> None:
        """Refit global and per-direction calibrators from current observations."""
        self._calibrator = _fit_single(self._calibration_obs, self.MIN_CALIBRATION_OBS, self.PLATT_THRESHOLD)
        self._calibrators_by_dir = {}
        for d, obs in self._calibration_obs_by_dir.items():
            c = _fit_single(obs, self.MIN_CALIBRATION_OBS, self.PLATT_THRESHOLD)
            if c is not None:
                self._calibrators_by_dir[d] = c

    def _load_calibration_data(self) -> None:
        if not self.calibration_log_path.exists():
            return
        try:
            with open(self.calibration_log_path) as f:
                for row in csv.DictReader(f):
                    p = float(row["model_p"])
                    a = float(row["actual_outcome"])
                    d = row.get("direction", "")
                    self._calibration_obs.append((p, a))
                    if d:
                        self._calibration_obs_by_dir.setdefault(d, []).append((p, a))
            self._fit_calibrator()
        except Exception as exc:
            self.calibration_load_error = str(exc)
            _log.warning("Calibration load failed — running uncalibrated: %s", exc)

    def _append_calibration_csv(self, model_p: float, actual_outcome: bool, direction: str) -> None:
        path = self.calibration_log_path
        path.parent.mkdir(parents=True, exist_ok=True)
        is_new = not path.exists()
        with open(path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["logged_at", "model_p", "actual_outcome", "direction"])
            if is_new:
                writer.writeheader()
            writer.writerow({
                "logged_at": datetime.utcnow().isoformat(),
                "model_p": round(model_p, 4),
                "actual_outcome": int(actual_outcome),
                "direction": direction,
            })


# ── Calibrator helpers ────────────────────────────────────────────────────────

def _fit_single(obs: list[tuple[float, float]], min_obs: int, platt_threshold: int) -> Any | None:
    if len(obs) < min_obs:
        return None
    X = np.array([p for p, _ in obs])
    y = np.array([a for _, a in obs])
    try:
        if len(obs) < platt_threshold:
            from sklearn.linear_model import LogisticRegression
            lr = LogisticRegression(C=1.0, solver="lbfgs")
            lr.fit(X.reshape(-1, 1), y)
            return lr
        else:
            from sklearn.isotonic import IsotonicRegression
            ir = IsotonicRegression(out_of_bounds="clip")
            ir.fit(X, y)
            return ir
    except Exception as exc:
        _log.warning("Calibrator fit failed: %s", exc)
        return None


def _predict(calibrator: Any, raw_p: float) -> float:
    try:
        if hasattr(calibrator, "predict_proba"):
            return float(np.clip(calibrator.predict_proba([[raw_p]])[0][1], 0.001, 0.999))
        return float(np.clip(calibrator.predict([raw_p])[0], 0.001, 0.999))
    except Exception as exc:
        _log.warning("Calibrator predict failed, returning raw_p: %s", exc)
        return raw_p


# ── KDE and fraction helpers ──────────────────────────────────────────────────

def _apply_kde(
    members: list[float],
    threshold: float,
    direction: str,
    threshold_high: float | None,
    fallback: float,
    weights: list[float] | None = None,
) -> float:
    try:
        # Degenerate spread (e.g. all-zero precipitation members on a dry day) makes
        # gaussian_kde singular → nan density. Fall back to the plain fraction.
        if float(np.std(members)) == 0.0:
            return fallback
        # Phase 4: per-member weights amplify the more skillful model's density.
        # Uniform weights are equivalent to no weights.
        w = np.asarray(weights, dtype=float) if weights is not None else None
        kde = gaussian_kde(members, bw_method="scott", weights=w)
        x_eval = np.linspace(min(members) - 20, max(members) + 20, 500)
        density = kde(x_eval)
        total = density.sum()
        if not np.isfinite(total) or total <= 0:
            return fallback
        density /= total
        if direction == "above":
            p = float(density[x_eval >= threshold].sum())
        elif direction == "below":
            p = float(density[x_eval <= threshold].sum())
        elif direction == "range" and threshold_high is not None:
            p = float(density[(x_eval >= threshold) & (x_eval <= threshold_high)].sum())
        elif direction == "equal":
            p = float(density[(x_eval >= threshold - 0.5) & (x_eval <= threshold + 0.5)].sum())
        else:
            return fallback
        if not np.isfinite(p):
            return fallback
        return float(np.clip(p, 0.001, 0.999))
    except Exception:
        return fallback


def _fraction_satisfying(
    members: list[float],
    threshold: float,
    direction: str,
    threshold_high: float | None = None,
    weights: list[float] | None = None,
) -> float:
    if not members:
        return 0.5
    if direction == "above":
        sat = [v > threshold for v in members]
    elif direction == "below":
        sat = [v < threshold for v in members]
    elif direction == "range" and threshold_high is not None:
        sat = [threshold <= v <= threshold_high for v in members]
    else:  # "equal"
        sat = [abs(v - threshold) <= 0.5 for v in members]
    if weights is not None:
        # Phase 4: weighted fraction = Σ w·[satisfies] / Σ w. Uniform weights ≡ unweighted.
        total_w = sum(weights)
        num = sum(w for s, w in zip(sat, weights) if s)
        frac = num / total_w if total_w else 0.5
    else:
        frac = sum(sat) / len(members)
    return float(np.clip(frac, 0.001, 0.999))
