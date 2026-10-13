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

import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import LIVE_EMOS_PERCELL_ENABLED, VARIANCE_INFLATION
from .paper_trader import PaperTrader
from .probability_model import (
    DispersionCorrector,
    HistoricalSkillCorrector,
    ProbabilityModel,
    StationForecastCorrector,
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
    calibration_halflife_days: float | None = None
    # Phase 3 T2 (docs/PHASE3_STATION_FORECAST_PLAN.md). Only "mosmix" is
    # meaningful today; the string (not a bool) leaves room for a future
    # second station-forecast source without another ModelSpec field.
    station_forecast: str | None = None

    def __post_init__(self) -> None:
        """MOSMIX IS the MOS correction for a spec that uses it — stacking the
        historical-skill shift on top would double-correct (see
        StationForecastCorrector's docstring). Normalized HERE, not just in
        build_model(), so the contradictory combination (mos_enabled=True,
        station_forecast="mosmix") can't be constructed at all — a spec that
        claims mos_enabled=True while actually running without MOS would be a
        silent lie about its own config."""
        if self.station_forecast == "mosmix" and self.mos_enabled:
            object.__setattr__(self, "mos_enabled", False)

    def build_model(self, log_dir: Path) -> ProbabilityModel:
        """Construct an isolated ProbabilityModel for this spec. MOS off ⇒ no
        skill corrector, so signal_generator falls back to flat city bias exactly
        as it would if MOS had no data (the honest 'MOS contributes nothing' arm).
        emos_percell ⇒ attach the per-cell dispersion corrector, which overrides
        the scalar λ where the skill table has a trusted cell. station_forecast
        ⇒ MOSMIX anchors the ensemble mean instead of MOS (__post_init__ already
        guarantees mos_enabled is False whenever this is set)."""
        corrector = HistoricalSkillCorrector() if self.mos_enabled else None
        dispersion = DispersionCorrector(base_lambda=self.variance_inflation) if self.emos_percell else None
        station = StationForecastCorrector() if self.station_forecast == "mosmix" else None
        cal_path = log_dir / "shadow" / self.name / "calibration_log.csv"
        return ProbabilityModel(
            calibration_log_path=cal_path,
            skill_corrector=corrector,
            model_weights=self.model_weights,
            variance_inflation=self.variance_inflation,
            variance_inflation_enabled=self.variance_inflation_enabled,
            dispersion_corrector=dispersion,
            calibration_halflife_days=self.calibration_halflife_days,
            station_forecast_corrector=station,
            name=self.name,
        )


# The seed roster. "prod_mirror" reproduces production — its Brier should track
# the live track's, which is how we know the harness itself is faithful. The
# others are deliberately known-different (λ=1.0 must look WORSE — that's the
# harness proving it can discriminate) plus a near-optimum probe. The proper
# per-cell EMOS challenger is appended once fit_emos.py produces its table.
DEFAULT_SPECS: list[ModelSpec] = [
    # Must track weather_bot.py's ACTUAL live construction, including its kill
    # switches — a hardcoded snapshot goes stale the next time production's
    # recipe changes. It did: Oct 5 2026 promoted emos_percell dispersion via
    # LIVE_EMOS_PERCELL_ENABLED and this literal wasn't updated, so prod_mirror
    # silently mirrored the PRE-promotion recipe for a week (caught via
    # compare_tracks.py's drift check — see [[skill_corrector_sentinel_bug]] for
    # the same staleness failure mode). Read live from config, not a literal.
    ModelSpec("prod_mirror", variance_inflation=VARIANCE_INFLATION, mos_enabled=True,
              emos_percell=LIVE_EMOS_PERCELL_ENABLED),
    ModelSpec("lambda_1_0", variance_inflation=1.0, variance_inflation_enabled=False),
    ModelSpec("lambda_2_5", variance_inflation=2.5),
    ModelSpec("mos_off", variance_inflation=2.0, mos_enabled=False),
    # The proper per-cell EMOS — the real candidate this harness exists to judge.
    ModelSpec("emos_percell", variance_inflation=2.0, emos_percell=True),
    # The pre-Oct-5 production recipe, pinned: scalar λ, MOS on, NO per-cell
    # dispersion. prod_mirror now tracks production (= emos_percell), so this is
    # the true counterfactual for the LIVE_EMOS_PERCELL_ENABLED promotion — the
    # only clean A/B of "did promoting emos_percell help". Deliberately a
    # literal (unlike prod_mirror): its whole point is to NOT follow the live
    # switch. Warm-starts from production's calibration_log like any new track.
    ModelSpec("scalar_lambda", variance_inflation=2.0, mos_enabled=True, emos_percell=False),
    # Recency-weighted calibrator (Workstream B): 30-day half-life so the calibrator
    # tracks the current regime instead of being outvoted by a stale summer history.
    # The candidate for the summer→autumn calibration drift (Sep 2026).
    ModelSpec("recency_cal", variance_inflation=2.0, calibration_halflife_days=30.0),
    # Phase 3 T2: DWD MOSMIX anchors the ensemble mean at stations it covers
    # (18/19 traded — KDAL/OMDB fall back to an unshifted ensemble, same as
    # mos_off, until T6 closes that gap). Compare against mos_off specifically
    # — same variance_inflation, only the mean-anchor source differs — for a
    # clean read on whether station calibration beats no calibration. T1's
    # go/no-go (scripts/mosmix_backcheck.py) is still open; this track exists
    # to accumulate T3's forward data in parallel, not to pre-empt T1 — do not
    # read its numbers as a verdict until T1 clears its own gate. (No
    # mos_enabled=False here — ModelSpec.__post_init__ normalizes it.)
    ModelSpec("mosmix", variance_inflation=2.0, station_forecast="mosmix"),
]


def _seed_calibration_log(log_dir: Path, track_dir: Path) -> bool:
    """Warm-start a NEW track's calibrator from production's calibration_log.

    Without this a shadow track starts cold, and a cold calibrator is not a
    model difference — it's an artifact. It made prod_mirror, whose whole job is
    to reproduce production, diverge by a mean 0.061 in model_p (max 0.259) and
    trade 66 markets production never saw (Sep 2026). While that holds, no
    challenger result can be read against production, which is the comparison
    that decides a promotion.

    Honest caveat: the seeded rows carry PRODUCTION's model_p against real
    outcomes, not the challenger's. For prod_mirror that is exact. For a genuine
    challenger it is a warm start — its calibrator begins from production's
    p→outcome mapping and diverges as its own resolutions accrue. That is the
    intended behaviour: a challenger should have to earn its way off the
    production prior, not be handicapped by an empty one. recency_cal still
    expresses its hypothesis, because it re-weights the same seeded history by age
    (and a 30-day half-life is meaningless on a log that starts today).

    Only seeds when the track has no calibration_log yet, so it happens once per
    track and never rewrites accrued history. Returns True if it seeded.
    """
    dest = track_dir / "calibration_log.csv"
    if dest.exists():
        return False
    src = log_dir / "calibration_log.csv"
    if not src.exists():
        return False
    try:
        shutil.copyfile(src, dest)
        return True
    except OSError as e:  # noqa: BLE001 — a shadow must never break the scan
        print(f"  [shadow] calibration seed failed for {track_dir.name}: {e}",
              file=sys.stderr)
        return False


class _AsOfPriceView:
    """Read-only Gate 6 view over PRODUCTION's PriceTracker, as of a given instant.

    Shadows used to run with price_tracker=None, silently skipping Gate 6 (odds
    velocity) — they were more permissive than production and took trades
    production's gate rejected (Oct 2026, [[emos_percell_gap_investigation]]).
    Two things make a naive shared tracker wrong:
      * record() must be a no-op — production already recorded this scan's
        price, and a second write per track would double-count the history.
      * production records a market's price AFTER its own gate check, so by the
        time a shadow runs, the newest sample is THIS scan's price. Production's
        velocity never saw it. Reading strictly before `as_of` (the production
        signal_time, which precedes that record) reproduces production's number.
    """

    def __init__(self, tracker):
        self._tracker = tracker
        self.as_of: datetime | None = None

    def record(self, market_id: str, yes_price: float) -> None:  # noqa: ARG002
        return None

    def get_velocity(self, market_id: str, window_hours: float) -> float | None:
        if self._tracker is None:
            return None
        as_of = self.as_of or datetime.now(timezone.utc)
        return self._tracker.velocity_between(
            market_id, as_of - timedelta(hours=window_hours), as_of)


class ShadowTracker:
    """Runs a roster of challenger models over each scan's production forecasts
    and logs each track's own qualifying signals. Built once per process and
    reused across scans (models load their calibrator once)."""

    def __init__(self, specs: list[ModelSpec], client, log_dir: Path):
        self.log_dir = log_dir
        # Gate 6 parity: None until attach_price_tracker() hands us production's
        # tracker (run_scan does, each scan — a resolve-only process never needs it).
        self._price_view = _AsOfPriceView(None)
        self.tracks: list[tuple[ModelSpec, SignalGenerator, PaperTrader]] = []
        for spec in specs:
            # Each track owns an isolated directory for its paper log + calibrator.
            # Create it up front — PaperTrader/ProbabilityModel append to files and
            # don't build nested parents themselves.
            track_dir = log_dir / "shadow" / spec.name
            track_dir.mkdir(parents=True, exist_ok=True)
            _seed_calibration_log(log_dir, track_dir)
            model = spec.build_model(log_dir)
            gen = SignalGenerator(model, client, price_tracker=self._price_view)
            paper = PaperTrader(log_path=track_dir / "paper_trades.csv")
            self.tracks.append((spec, gen, paper))

    def attach_price_tracker(self, tracker) -> None:
        """Point Gate 6 at production's PriceTracker (read-only). Idempotent;
        until called (or with None) shadows skip Gate 6 like velocity-less data."""
        self._price_view._tracker = tracker

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
                    self._price_view.as_of = getattr(s, "signal_time", None)
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
