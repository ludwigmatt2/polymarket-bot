"""Shadow A/B harness: challenger models re-score the production forecast in
isolation, log to their own files, and never perturb the scan."""

import csv
from datetime import date, datetime, timezone, timedelta
from unittest.mock import MagicMock

import pytest

from weather.models import EnsembleForecast, Location, WeatherMarket
from weather.probability_model import ProbabilityModel
from weather.shadow import DEFAULT_SPECS, ModelSpec, ShadowTracker
from weather.signal_generator import SignalGenerator


def _market(yes_price=0.36):
    return WeatherMarket(
        market_id="shadow_mkt",
        title="Will the high temperature in Miami exceed 90F?",
        yes_price=yes_price,
        liquidity_usd=5000.0,
        resolution_date=datetime.now(timezone.utc) + timedelta(days=3),
        resolution_source="NOAA",
        location=Location(city="Miami", lat=25.77, lon=-80.19),
        metric="temperature_2m_max",
        threshold=90.0,
        direction="above",
        url="https://polymarket.com/test",
        station_icao="KMIA",
        station_country="US",
        resolve_unit="F",
    )


def _forecast():
    # Members centered ON the 90F threshold → P(above)≈0.5 for any λ (inflation
    # preserves the mean). At yes_price 0.36 that's a ~0.14 edge — inside the
    # [0.08, 0.20] gate band, so a signal logs in every track.
    return EnsembleForecast(
        lat=25.77, lon=-80.19,
        target_date=date(2026, 5, 5),
        metric="temperature_2m_max",
        member_arrays={"gfs_seamless": [86.0, 87.0, 88.0, 89.0, 90.0, 91.0, 92.0, 93.0, 94.0] * 3,
                       "ecmwf_ifs025": [86.5, 87.5, 88.5, 89.5, 90.5, 91.5, 92.5, 93.5, 94.5] * 3},
        model_means={"gfs_seamless": 90.0, "ecmwf_ifs025": 90.5},
        fetched_at=datetime.now(timezone.utc),
    )


def _production_signals(tmp_path):
    model = ProbabilityModel(calibration_log_path=tmp_path / "prod_cal.csv", skill_corrector=None)
    gen = SignalGenerator(model=model, client=MagicMock())
    return [gen.evaluate(_market(), forecast=_forecast())]


# ── spec / model isolation ────────────────────────────────────────────────────
def test_spec_builds_model_with_its_lambda_and_isolated_calibration(tmp_path):
    a = ModelSpec("a", variance_inflation=1.0).build_model(tmp_path)
    b = ModelSpec("b", variance_inflation=2.5).build_model(tmp_path)
    assert a.variance_inflation == 1.0 and b.variance_inflation == 2.5
    assert a.calibration_log_path != b.calibration_log_path
    assert a.name == "a" and b.name == "b"


def test_prod_mirror_matches_a_default_model(tmp_path):
    """The mirror must reproduce production — else the harness isn't faithful."""
    prod = ProbabilityModel(calibration_log_path=tmp_path / "p.csv", skill_corrector=None)
    mirror = ModelSpec("prod_mirror", variance_inflation=2.0).build_model(tmp_path)
    mirror.skill_corrector = None  # match the no-MOS default model above
    fc = _forecast()
    assert mirror.compute_probability(fc, 98.0, "above").raw_p == \
        pytest.approx(prod.compute_probability(fc, 98.0, "above").raw_p, abs=1e-9)


def test_lambda_discriminates_on_a_tail_bucket(tmp_path):
    """The harness must tell configs apart: λ=1 gives a thinner tail than λ=2.5."""
    fc = _forecast()
    lo = ModelSpec("lo", variance_inflation=1.0, variance_inflation_enabled=False).build_model(tmp_path)
    hi = ModelSpec("hi", variance_inflation=2.5).build_model(tmp_path)
    # A bucket above the top member: only reachable once the spread is inflated.
    assert hi.compute_probability(fc, 99.0, "above").raw_p > \
        lo.compute_probability(fc, 99.0, "above").raw_p


# ── Phase 3 T2: mosmix spec ────────────────────────────────────────────────────
def test_mosmix_spec_attaches_station_forecast_corrector(tmp_path):
    from weather.probability_model import StationForecastCorrector
    spec = ModelSpec("mosmix", station_forecast="mosmix")
    model = spec.build_model(tmp_path)
    assert isinstance(model.station_forecast_corrector, StationForecastCorrector)


def test_mosmix_spec_forces_mos_off_even_if_mos_enabled_true(tmp_path):
    """MOSMIX IS the MOS correction for this spec — stacking the historical-skill
    shift on top would double-correct. build_model must force this regardless of
    what mos_enabled says, so a future edit to the DEFAULT_SPECS entry can't
    silently reintroduce the double-correction."""
    spec = ModelSpec("mosmix", station_forecast="mosmix", mos_enabled=True)
    model = spec.build_model(tmp_path)
    assert model.skill_corrector is None


def test_non_mosmix_spec_has_no_station_forecast_corrector(tmp_path):
    spec = ModelSpec("plain", station_forecast=None)
    model = spec.build_model(tmp_path)
    assert model.station_forecast_corrector is None


def test_default_specs_include_mosmix_paired_with_mos_off():
    names = {s.name: s for s in DEFAULT_SPECS}
    assert "mosmix" in names
    mosmix, mos_off = names["mosmix"], names["mos_off"]
    assert mosmix.station_forecast == "mosmix"
    # Same variance_inflation as mos_off so mosmix-vs-mos_off isolates the mean-
    # anchor source, not an incidental λ difference (see DEFAULT_SPECS comment).
    assert mosmix.variance_inflation == mos_off.variance_inflation


# ── tracker mechanics ─────────────────────────────────────────────────────────
def test_evaluate_and_log_writes_isolated_files(tmp_path):
    # mos_enabled=False so the test doesn't depend on a local skill table shifting
    # the estimate out of the gate band — we're testing the harness, not MOS.
    specs = [ModelSpec("alpha", variance_inflation=2.0, mos_enabled=False),
             ModelSpec("beta", variance_inflation=1.0, variance_inflation_enabled=False, mos_enabled=False)]
    tracker = ShadowTracker(specs, client=MagicMock(), log_dir=tmp_path)
    counts = tracker.evaluate_and_log(_production_signals(tmp_path))
    assert counts == {"alpha": 1, "beta": 1}
    for name in ("alpha", "beta"):
        p = tmp_path / "shadow" / name / "paper_trades.csv"
        assert p.exists()
        assert len(list(csv.DictReader(open(p)))) == 1


def test_tracker_never_raises_on_bad_signal(tmp_path):
    tracker = ShadowTracker([ModelSpec("x")], client=MagicMock(), log_dir=tmp_path)
    bad = MagicMock()
    bad.forecast = None            # missing forecast → skipped, not crashed
    bad.market = None
    counts = tracker.evaluate_and_log([bad])
    assert counts == {"x": 0}


def test_default_roster_present():
    names = [s.name for s in DEFAULT_SPECS]
    assert "prod_mirror" in names and "lambda_1_0" in names


# ── Calibrator warm-start (T2) ────────────────────────────────────────────────
# A cold shadow calibrator is not a model difference, it's an artifact: it made
# prod_mirror diverge from production by a mean 0.061 in model_p (Sep 2026) and
# so made every challenger incomparable to production. New tracks warm-start
# from production's calibration_log instead.

def _write_prod_cal(log_dir, n=3):
    log_dir.mkdir(parents=True, exist_ok=True)
    p = log_dir / "calibration_log.csv"
    with open(p, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["logged_at", "model_p", "actual_outcome", "direction"])
        w.writeheader()
        for i in range(n):
            w.writerow({"logged_at": f"2026-09-0{i + 1}T00:00:00",
                        "model_p": 0.4 + i / 100, "actual_outcome": i % 2, "direction": "YES"})
    return p


def test_new_track_seeds_calibration_from_production(tmp_path):
    src = _write_prod_cal(tmp_path)
    ShadowTracker([ModelSpec("fresh")], client=MagicMock(), log_dir=tmp_path)
    seeded = tmp_path / "shadow" / "fresh" / "calibration_log.csv"
    assert seeded.exists()
    assert seeded.read_text() == src.read_text()


def test_seeding_never_overwrites_accrued_history(tmp_path):
    """Seeding is once-per-track. A second construction (every process restart)
    must not clobber calibration the track has since learned for itself."""
    _write_prod_cal(tmp_path)
    ShadowTracker([ModelSpec("fresh")], client=MagicMock(), log_dir=tmp_path)
    seeded = tmp_path / "shadow" / "fresh" / "calibration_log.csv"
    seeded.write_text(seeded.read_text() + "2026-09-09T00:00:00,0.77,1,NO\n")
    after = seeded.read_text()
    ShadowTracker([ModelSpec("fresh")], client=MagicMock(), log_dir=tmp_path)
    assert seeded.read_text() == after


def test_seeding_is_a_noop_without_a_production_log(tmp_path):
    """Fresh install, nothing to warm-start from — must not crash the scan."""
    ShadowTracker([ModelSpec("fresh")], client=MagicMock(), log_dir=tmp_path)
    assert not (tmp_path / "shadow" / "fresh" / "calibration_log.csv").exists()


def test_seeded_obs_reach_the_track_calibrator(tmp_path):
    """The point of seeding: the model actually LOADS the warm-start rows, so a
    challenger starts from production's p→outcome mapping rather than empty."""
    _write_prod_cal(tmp_path, n=5)
    tracker = ShadowTracker([ModelSpec("fresh", mos_enabled=False)],
                            client=MagicMock(), log_dir=tmp_path)
    _, gen, _ = tracker.tracks[0]
    assert gen.model.n_calibration_obs == 5
