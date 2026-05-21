"""
Ensemble → calibrated P(outcome) model.

Core logic:
1. Raw probability: fraction of ensemble members satisfying condition
2. Gaussian KDE smoothing to reduce discretization artifacts
3. Isotonic regression calibration once 50+ observations are collected
4. Uncertainty gate: reject signals when cross-model spread is too high
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

import numpy as np
from scipy.stats import gaussian_kde

from .config import MAX_ENSEMBLE_SPREAD, MIN_ENSEMBLE_MEMBERS
from .models import EnsembleForecast, RawProbabilityResult


class ProbabilityModel:
    """
    Converts raw ensemble data into calibrated P(YES) for a binary weather outcome.

    Calibration is disabled until MIN_CALIBRATION_OBS resolved observations are
    stored in calibration_log_path. After that, isotonic regression is fitted and
    applied on every call.
    """

    MIN_CALIBRATION_OBS = 50
    CLIMATOLOGY_BRIER = 0.25  # Baseline for a 50/50 uninformed forecast

    def __init__(self, calibration_log_path: Path = Path("logs/calibration_log.csv")):
        self.calibration_log_path = calibration_log_path
        self._calibrator = None   # sklearn IsotonicRegression, fitted lazily
        self._calibration_obs: list[tuple[float, float]] = []  # (model_p, actual)
        self._load_calibration_data()

    def compute_probability(
        self,
        forecast: EnsembleForecast,
        threshold: float,
        direction: str,
        threshold_high: float | None = None,
    ) -> RawProbabilityResult:
        """
        Full pipeline: raw P → KDE smoothing → calibration (if available).

        Args:
            forecast: EnsembleForecast with member arrays
            threshold: e.g. 90.0 (degrees F)
            direction: "above" or "below"
        """
        members = forecast.all_members
        if not members:
            return RawProbabilityResult(
                raw_p=0.5, calibrated_p=0.5,
                ensemble_spread=1.0, n_members=0,
                is_calibrated=False,
                model_breakdown={}, threshold=threshold,
                direction=direction, metric=forecast.metric,
            )

        # Per-model breakdown for transparency
        model_breakdown: dict[str, float] = {}
        for model, model_members in forecast.member_arrays.items():
            if model_members:
                model_breakdown[model] = _fraction_satisfying(
                    model_members, threshold, direction, threshold_high
                )

        # Raw probability from full member pool
        raw_p = _fraction_satisfying(members, threshold, direction, threshold_high)

        if len(members) >= 10:
            raw_p = _apply_kde(members, threshold, direction, threshold_high, raw_p)

        calibrated_p = self._apply_calibration(raw_p)

        # Spread = std of per-model probabilities (probability units, 0–0.5)
        # This is independent of the metric's unit (°C, mm, etc.)
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
            is_calibrated=self._calibrator is not None,
            model_breakdown=model_breakdown,
            threshold=threshold,
            direction=direction,
            metric=forecast.metric,
        )

    @property
    def n_calibration_obs(self) -> int:
        return len(self._calibration_obs)

    def is_confident(self, result: RawProbabilityResult) -> bool:
        """Returns True if forecast quality passes all uncertainty gates."""
        if result.n_members < MIN_ENSEMBLE_MEMBERS:
            return False
        if result.ensemble_spread > MAX_ENSEMBLE_SPREAD:
            return False
        return True

    def log_observation(self, model_p: float, actual_outcome: bool) -> None:
        """
        Record a resolved observation for calibration.
        Writes to calibration_log_path and refits calibrator if threshold reached.
        """
        obs = (model_p, float(actual_outcome))
        self._calibration_obs.append(obs)
        self._append_calibration_csv(model_p, actual_outcome)
        if len(self._calibration_obs) >= self.MIN_CALIBRATION_OBS:
            self._fit_calibrator()

    def _apply_calibration(self, raw_p: float) -> float:
        if self._calibrator is None:
            return raw_p
        try:
            return float(np.clip(self._calibrator.predict([[raw_p]])[0], 0.001, 0.999))
        except Exception:
            return raw_p

    def _fit_calibrator(self) -> None:
        try:
            from sklearn.isotonic import IsotonicRegression
            X = np.array([[p] for p, _ in self._calibration_obs])
            y = np.array([a for _, a in self._calibration_obs])
            ir = IsotonicRegression(out_of_bounds="clip")
            ir.fit(X.ravel(), y)
            self._calibrator = ir
        except Exception:
            pass

    def _load_calibration_data(self) -> None:
        if not self.calibration_log_path.exists():
            return
        try:
            with open(self.calibration_log_path) as f:
                for row in csv.DictReader(f):
                    p = float(row["model_p"])
                    a = float(row["actual_outcome"])
                    self._calibration_obs.append((p, a))
            if len(self._calibration_obs) >= self.MIN_CALIBRATION_OBS:
                self._fit_calibrator()
        except Exception:
            pass

    def _append_calibration_csv(self, model_p: float, actual_outcome: bool) -> None:
        path = self.calibration_log_path
        is_new = not path.exists()
        with open(path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["logged_at", "model_p", "actual_outcome"])
            if is_new:
                writer.writeheader()
            writer.writerow({
                "logged_at": datetime.utcnow().isoformat(),
                "model_p": round(model_p, 4),
                "actual_outcome": int(actual_outcome),
            })


def _apply_kde(
    members: list[float],
    threshold: float,
    direction: str,
    threshold_high: float | None,
    fallback: float,
) -> float:
    """KDE-smoothed probability estimate. Returns fallback if KDE fails."""
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
    """
    Fraction of members satisfying the condition.

    direction:
      "above"  → P(T > threshold)
      "below"  → P(T < threshold)
      "equal"  → P(T in [threshold-0.5, threshold+0.5])
      "range"  → P(threshold ≤ T ≤ threshold_high)
    """
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
