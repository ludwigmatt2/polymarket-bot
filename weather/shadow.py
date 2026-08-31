"""Shadow model tracks — forward A/B of challenger model configs.

The production model is one point in a space of choices (variance-inflation λ,
MOS on/off, model weights, …). A shadow track re-scores the SAME live ensemble
forecast under a challenger config and logs its own paper trades to a separate
file, so a change can be judged on out-of-sample forward data before it ever
touches the production signal path — let alone real money.

Invariants (why this is safe to run in the live scan):
  * Challengers only ever READ the production forecast (passed into evaluate);
    they never fetch, never place orders, never write to the production logs.
  * The whole harness is wrapped so any challenger error is swallowed — a shadow
    track can fail without perturbing the scan that funds real trades.
  * Each track owns an isolated calibrator + calibration_log, so calibration
    learned on one config's raw_p never leaks into another's.

The matching OFFLINE re-score (scripts/compare_tracks.py) scores these same
specs over the resolved history for an instant in-sample read; this module is
the forward, out-of-sample confirmation. Use both: offline picks candidates,
forward confirms them (see [[equity_bankroll_and_skip_funnel]] session).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

from .paper_trader import PaperTrader
from .probability_model import (
    DispersionCorrector,
    HistoricalSkillCorrector,
    ProbabilityModel,
)
from .signal_generator import SignalGenerator


@dataclass(frozen=True)
class ModelSpec:
    """A named challenger model configuration. Only the knobs that change model
    OUTPUT live here; execution/sizing is never part of a shadow track."""

    name: str
    variance_inflation: float = 2.0
    variance_inflation_enabled: bool = True
    mos_enabled: bool = True
    emos_percell: bool = False
    model_weights: dict[str, float] | None = None

    def build_model(self, log_dir: Path) -> ProbabilityModel:
        """Construct an isolated ProbabilityModel for this spec. MOS off ⇒ no
        skill corrector, so signal_generator falls back to flat city bias exactly
        as it would if MOS had no data (the honest 'MOS contributes nothing' arm).
        emos_percell ⇒ attach the per-cell dispersion corrector, which overrides
        the scalar λ where the skill table has a trusted cell."""
        corrector = HistoricalSkillCorrector() if self.mos_enabled else None
        dispersion = DispersionCorrector(base_lambda=self.variance_inflation) if self.emos_percell else None
        cal_path = log_dir / "shadow" / self.name / "calibration_log.csv"
        return ProbabilityModel(
            calibration_log_path=cal_path,
            skill_corrector=corrector,
            model_weights=self.model_weights,
            variance_inflation=self.variance_inflation,
            variance_inflation_enabled=self.variance_inflation_enabled,
            dispersion_corrector=dispersion,
            name=self.name,
        )


# The seed roster. "prod_mirror" reproduces production — its Brier should track
# the live track's, which is how we know the harness itself is faithful. The
# others are deliberately known-different (λ=1.0 must look WORSE — that's the
# harness proving it can discriminate) plus a near-optimum probe. The proper
# per-cell EMOS challenger is appended once fit_emos.py produces its table.
DEFAULT_SPECS: list[ModelSpec] = [
    ModelSpec("prod_mirror", variance_inflation=2.0, mos_enabled=True),
    ModelSpec("lambda_1_0", variance_inflation=1.0, variance_inflation_enabled=False),
    ModelSpec("lambda_2_5", variance_inflation=2.5),
    ModelSpec("mos_off", variance_inflation=2.0, mos_enabled=False),
    # The proper per-cell EMOS — the real candidate this harness exists to judge.
    ModelSpec("emos_percell", variance_inflation=2.0, emos_percell=True),
]


class ShadowTracker:
    """Runs a roster of challenger models over each scan's production forecasts
    and logs each track's own qualifying signals. Built once per process and
    reused across scans (models load their calibrator once)."""

    def __init__(self, specs: list[ModelSpec], client, log_dir: Path):
        self.log_dir = log_dir
        self.tracks: list[tuple[ModelSpec, SignalGenerator, PaperTrader]] = []
        for spec in specs:
            # Each track owns an isolated directory for its paper log + calibrator.
            # Create it up front — PaperTrader/ProbabilityModel append to files and
            # don't build nested parents themselves.
            track_dir = log_dir / "shadow" / spec.name
            track_dir.mkdir(parents=True, exist_ok=True)
            model = spec.build_model(log_dir)
            gen = SignalGenerator(model, client)
            paper = PaperTrader(log_path=track_dir / "paper_trades.csv")
            self.tracks.append((spec, gen, paper))

    def evaluate_and_log(self, production_signals: list) -> dict[str, int]:
        """Re-score every market the production scan evaluated under each
        challenger, using the production forecast each Signal already carries (no
        refetch, identical ensemble). Log the signals that pass THAT model's gate.
        Returns {track_name: n_logged}. Never raises."""
        counts: dict[str, int] = {}
        for spec, gen, paper in self.tracks:
            n = 0
            for s in production_signals:
                fc = getattr(s, "forecast", None)
                mkt = getattr(s, "market", None)
                if fc is None or mkt is None:
                    continue
                try:
                    sig = gen.evaluate(mkt, forecast=fc)
                    if sig.quality_gate_passed and paper.log_trade(sig):
                        n += 1
                except Exception as e:  # noqa: BLE001 — a shadow must never break the scan
                    print(f"  [shadow:{spec.name}] eval error: {e}", file=sys.stderr)
                    continue
            counts[spec.name] = n
        return counts

    def resolve_all(self, weather_client) -> dict[str, int]:
        """Resolve each track's paper trades and feed its OWN calibrator (each
        track has an isolated calibration_log, so this can't cross-contaminate).
        Returns {track_name: n_resolved}. Never raises."""
        out: dict[str, int] = {}
        for spec, gen, paper in self.tracks:
            try:
                resolved, _ = paper.auto_resolve(weather_client, model=gen.model)
                out[spec.name] = resolved
            except Exception as e:  # noqa: BLE001
                print(f"  [shadow:{spec.name}] resolve error: {e}", file=sys.stderr)
                out[spec.name] = 0
        return out
