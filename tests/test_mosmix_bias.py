"""Per-station MOSMIX bias correction (T1 follow-up, Oct 2026)."""
import csv
import json
from datetime import date

import pytest

from weather import iem_client, mosmix_bias, mosmix_client
from weather.mosmix_bias import MosmixBiasTable, fit_table, refit_from_archive, shrunk_bias
from weather.probability_model import StationForecastCorrector
from weather.shadow import DEFAULT_SPECS, ModelSpec


def test_shrunk_bias_counts_days_not_rows():
    # one day with 5 lead-rows must count as ONE day
    assert shrunk_bias({"2026-10-01": [1.0] * 5}, k=10, min_days=2) is None
    est = shrunk_bias({f"2026-10-0{d}": [2.0, 2.0] for d in range(1, 8)}, k=10, min_days=7)
    bias, n = est
    assert n == 7 and bias == pytest.approx(7 / 17 * 2.0)


def test_shrunk_bias_stands_down_below_min_days():
    assert shrunk_bias({"2026-10-01": [1.0], "2026-10-02": [1.0]}, k=10, min_days=7) is None


def _rows(icao="EGLC", kind="max", err=1.0, days=8):
    return [{"icao": icao, "target_day": f"2026-10-{d:02d}", "kind": kind,
             "mosmix_mean_c": str(20.0 + err)} for d in range(1, days + 1)]


def test_fit_table_uses_only_past_days_with_truth():
    rows = _rows(days=8) + [{"icao": "EGLC", "target_day": "2026-10-20", "kind": "max",
                             "mosmix_mean_c": "99"}]
    truth = {("EGLC", f"2026-10-{d:02d}"): {"max": 20.0} for d in range(1, 9)}
    truth[("EGLC", "2026-10-20")] = {"max": 0.0}
    t = fit_table(rows, truth, today=date(2026, 10, 10), k=10, min_days=7)
    e = t["entries"]["EGLC|max"]
    assert e["n_days"] == 8                      # the future row is excluded
    assert e["bias_c"] == pytest.approx(8 / 18 * 1.0, abs=1e-3)


def test_table_missing_or_corrupt_file_never_raises(tmp_path):
    assert len(MosmixBiasTable.load(tmp_path / "nope.json")) == 0
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert MosmixBiasTable.load(bad).lookup("EGLC", "max") is None


def _patch_mosmix(monkeypatch, mean=20.0):
    monkeypatch.setattr(iem_client, "icao_for_latlon", lambda lat, lon: "EGLC")
    monkeypatch.setattr(iem_client, "station_meta", lambda i: {"wmo": "P0478", "tz": "Europe/London"})
    monkeypatch.setattr(mosmix_client, "fetch_station", lambda wmo: object())
    monkeypatch.setattr(mosmix_client, "daily_extreme", lambda fc, d, tz, kind: (mean, 0.7))


def test_corrector_subtracts_learned_bias(monkeypatch):
    _patch_mosmix(monkeypatch, mean=20.0)
    table = MosmixBiasTable({"EGLC|max": {"bias_c": -1.3, "n_days": 12}})
    raw = StationForecastCorrector().target_mean(51.5, 0.05, "temperature_2m_max", date(2026, 10, 14))
    bc = StationForecastCorrector(bias_table=table).target_mean(
        51.5, 0.05, "temperature_2m_max", date(2026, 10, 14))
    assert raw == 20.0
    assert bc == pytest.approx(21.3)             # MOSMIX ran cold by 1.3 -> shift up


def test_corrector_without_entry_is_unchanged(monkeypatch):
    _patch_mosmix(monkeypatch, mean=20.0)
    bc = StationForecastCorrector(bias_table=MosmixBiasTable({})).target_mean(
        51.5, 0.05, "temperature_2m_max", date(2026, 10, 14))
    assert bc == 20.0


def test_mosmix_bc_spec_pairs_with_mosmix_and_loads_table(tmp_path):
    specs = {s.name: s for s in DEFAULT_SPECS}
    assert specs["mosmix_bc"].station_bias_correction is True
    assert specs["mosmix"].station_bias_correction is False
    assert specs["mosmix_bc"].variance_inflation == specs["mosmix"].variance_inflation
    (tmp_path / "mosmix_bias.json").write_text(
        json.dumps({"entries": {"KATL|max": {"bias_c": 1.4, "n_days": 10}}}))
    m = ModelSpec("mosmix_bc", station_forecast="mosmix",
                  station_bias_correction=True).build_model(tmp_path)
    assert m.station_forecast_corrector.bias_table.lookup("KATL", "max") == 1.4
    plain = ModelSpec("mosmix", station_forecast="mosmix").build_model(tmp_path)
    assert plain.station_forecast_corrector.bias_table is None


def test_refit_from_archive_writes_atomically(tmp_path, monkeypatch):
    arch = tmp_path / "arch.csv"
    rows = _rows(icao="KATL", kind="max", err=1.5, days=9)
    with open(arch, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["icao", "target_day", "kind", "mosmix_mean_c"])
        w.writeheader()
        w.writerows(rows)
    monkeypatch.setattr(iem_client, "daily_range", lambda icao, a, b: {
        f"2026-10-{d:02d}": {"max_f": 68.0, "min_f": None} for d in range(1, 10)})  # 20.0 C
    out = tmp_path / "bias.json"
    n = refit_from_archive(arch, out, today=date(2026, 10, 15))
    assert n == 1 and out.exists() and not (tmp_path / "bias.json.tmp").exists()
    assert MosmixBiasTable.load(out).lookup("KATL", "max") == pytest.approx(9 / 19 * 1.5, abs=1e-3)


def test_refit_without_archive_is_a_noop(tmp_path):
    assert refit_from_archive(tmp_path / "missing.csv", tmp_path / "o.json") == 0
