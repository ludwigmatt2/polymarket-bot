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
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

import numpy as np
from scipy.stats import gaussian_kde

from .config import MAX_ENSEMBLE_SPREAD, MIN_ENSEMBLE_MEMBERS
from .models import EnsembleForecast, RawProbabilityResult


class ProbabilityModel:
    MIN_CALIBRATION_OBS = 30    # minimum to activate any calibrator
    PLATT_THRESHOLD = 300       # switch from Platt to isotonic above this
    REFIT_INTERVAL = 10         # refit calibrators every N new observations

    def __init__(self, calibration_log_path: Path = Path("logs/calibration_log.csv")):
        self.calibration_log_path = calibration_log_path
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
    ) -> RawProbabilityResult:
        """Full pipeline: raw P → KDE smoothing → calibration (direction-aware)."""
        members = forecast.all_members
        if not members:
            return RawProbabilityResult(
                raw_p=0.5, calibrated_p=0.5,
                ensemble_spread=1.0, n_members=0,
                is_calibrated=False,
                model_breakdown={}, threshold=threshold,
                direction=direction, metric=forecast.metric,
            )

        model_breakdown: dict[str, float] = {}
        for model, model_members in forecast.member_arrays.items():
            if model_members:
                model_breakdown[model] = _fraction_satisfying(
                    model_members, threshold, direction, threshold_high
                )

        raw_p = _fraction_satisfying(members, threshold, direction, threshold_high)

        if len(members) >= 10:
            raw_p = _apply_kde(members, threshold, direction, threshold_high, raw_p)

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
) -> float:
    try:
        kde = gaussian_kde(members, bw_method="scott")
        x_eval = np.linspace(min(members) - 20, max(members) + 20, 500)
        density = kde(x_eval)
        density /= density.sum()
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
        return float(np.clip(p, 0.001, 0.999))
    except Exception:
        return fallback


def _fraction_satisfying(
    members: list[float],
    threshold: float,
    direction: str,
    threshold_high: float | None = None,
) -> float:
    if not members:
        return 0.5
    if direction == "above":
        count = sum(1 for v in members if v > threshold)
    elif direction == "below":
        count = sum(1 for v in members if v < threshold)
    elif direction == "range" and threshold_high is not None:
        count = sum(1 for v in members if threshold <= v <= threshold_high)
    else:  # "equal"
        count = sum(1 for v in members if abs(v - threshold) <= 0.5)
    return float(np.clip(count / len(members), 0.001, 0.999))
