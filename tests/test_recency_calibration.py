"""Tests for the recency-weighted calibrator (Workstream B, `recency_cal`).

The calibrator fits on ALL accumulated observations. Without recency weighting a
stale summer-heavy history outvotes a handful of recent autumn observations, so
the model tracks a regime shift only glacially. `calibration_halflife_days` weights
each obs by 0.5**(age_days/halflife) so the current regime dominates.
"""

from __future__ import annotations

import csv
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from weather.probability_model import (
    ProbabilityModel,
    _predict,
    _recency_weights,
)
from weather.shadow import DEFAULT_SPECS, ModelSpec


def _write_cal_log(path: Path, rows: list[tuple[str, float, int, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["logged_at", "model_p", "actual_outcome", "direction"])
        w.writeheader()
        for ts, p, a, d in rows:
            w.writerow({"logged_at": ts, "model_p": p, "actual_outcome": a, "direction": d})


class TestRecencyWeights:
    def test_newest_weighted_one_halflife_old_weighted_half(self):
        now = datetime.utcnow()
        ts = [now - timedelta(days=30), now]  # one halflife apart
        w = _recency_weights(ts, halflife_days=30.0)
        assert w is not None
        assert w[1] == pytest.approx(1.0)      # newest
        assert w[0] == pytest.approx(0.5)      # 30 days = one halflife

    def test_none_when_halflife_nonpositive_or_missing_ts(self):
        now = datetime.utcnow()
        assert _recency_weights([now, now], 0.0) is None
        assert _recency_weights([now, now], -5.0) is None
        assert _recency_weights([now, None], 30.0) is None   # partial stamping never skews
        assert _recency_weights([], 30.0) is None


class TestCalibratorRegimeShift:
    """Old obs say 'raw_p 0.3 rarely resolves YES', recent obs say it usually does.
    The recency fit must lean toward the recent (current-regime) truth."""

    def _model(self, tmp_path: Path, halflife, name):
        log = tmp_path / f"cal_{name}.csv"
        now = datetime.utcnow()
        rows = []
        # 40 OLD obs (~60 days ago): model_p≈0.30 → outcome 0 (YES did not happen)
        for i in range(40):
            rows.append(((now - timedelta(days=60)).isoformat(), 0.30, 0, ""))
        # 40 RECENT obs (~now): model_p≈0.30 → outcome 1 (regime flipped)
        for i in range(40):
            rows.append((now.isoformat(), 0.30, 1, ""))
        _write_cal_log(log, rows)
        return ProbabilityModel(calibration_log_path=log, calibration_halflife_days=halflife,
                                name=name)

    def test_recency_fit_follows_recent_regime(self, tmp_path):
        unweighted = self._model(tmp_path, None, "flat")
        recency = self._model(tmp_path, 20.0, "recency")
        assert unweighted._calibrator is not None and recency._calibrator is not None
        p_flat = _predict(unweighted._calibrator, 0.30)
        p_recency = _predict(recency._calibrator, 0.30)
        # Even split (40 zeros / 40 ones) → unweighted sits near 0.5; recency, with
        # the recent ones dominating, must sit clearly higher (toward outcome=1).
        assert p_recency > p_flat + 0.15
        assert p_recency > 0.6

    def test_halflife_none_is_equal_weighted(self, tmp_path):
        """None must reproduce the pre-feature behaviour: no recency skew."""
        m = self._model(tmp_path, None, "none")
        # 40/40 split at one raw_p → equal-weighted fit predicts ≈ 0.5.
        assert _predict(m._calibrator, 0.30) == pytest.approx(0.5, abs=0.1)


class TestRecencyCalSpec:
    def test_recency_cal_in_default_roster(self):
        spec = next((s for s in DEFAULT_SPECS if s.name == "recency_cal"), None)
        assert spec is not None
        assert spec.calibration_halflife_days == 30.0

    def test_spec_builds_model_carrying_halflife(self, tmp_path):
        spec = ModelSpec("t", calibration_halflife_days=14.0)
        model = spec.build_model(tmp_path)
        assert model.calibration_halflife_days == 14.0
