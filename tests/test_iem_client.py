"""Tests for weather.iem_client — station-observation ground-truth client."""
from datetime import date, datetime

import pytest

from weather import iem_client as iem


@pytest.fixture(autouse=True)
def _no_throttle(monkeypatch):
    monkeypatch.setattr(iem, "_throttle", lambda: None)


class _Resp:
    def __init__(self, body):
        self._b = body.encode()
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return self._b


def _mock_urlopen(monkeypatch, body, capture=None):
    def fake(req, timeout=20):
        if capture is not None:
            capture["url"] = req.full_url
        return _Resp(body)
    monkeypatch.setattr("urllib.request.urlopen", fake)


def test_station_meta_us_and_intl():
    m = iem.station_meta("KLGA")
    assert m["network"] == "NY_ASOS" and m["sid"] == "LGA" and m["tz"] == "America/New_York"
    assert m["lat"] == pytest.approx(40.78, abs=0.05) and m["lon"] == pytest.approx(-73.88, abs=0.05)
    # international keeps full ICAO and uses a two-underscore network
    assert iem.station_meta("rksi")["sid"] == "RKSI"
    assert iem.station_meta("RKSI")["network"] == "KR__ASOS"
    assert iem.station_meta("ZZZZ") is None


def test_is_us():
    assert iem.is_us("KLGA") is True
    assert iem.is_us("VHHH") is False
    assert iem.is_us("ZZZZ") is False


def test_daily_maxmin_parses(monkeypatch):
    cap = {}
    _mock_urlopen(monkeypatch,
                  '[{"station":"LGA","day":"2026-07-04T00:00:00.000","max_temp_f":97.0,"min_temp_f":74.0}]',
                  cap)
    r = iem.daily_maxmin("KLGA", date(2026, 7, 4))
    assert r == {"max_f": 97.0, "min_f": 74.0}
    assert "network=NY_ASOS" in cap["url"] and "stations=LGA" in cap["url"]


def test_daily_maxmin_empty(monkeypatch):
    _mock_urlopen(monkeypatch, "[]")
    assert iem.daily_maxmin("KLGA", date(2026, 7, 4)) is None


def test_metar_peak_max_and_min(monkeypatch):
    csv = ("station,valid,tmpf\n"
           "MIA,2026-07-04 12:00,88.0\n"
           "MIA,2026-07-04 13:53,91.0\n"
           "MIA,2026-07-04 18:00,M\n"     # missing → skipped
           "MIA,2026-07-04 06:00,79.0\n")
    _mock_urlopen(monkeypatch, csv)
    assert iem.metar_peak("KMIA", date(2026, 7, 4), "max") == 91.0
    _mock_urlopen(monkeypatch, csv)
    assert iem.metar_peak("KMIA", date(2026, 7, 4), "min") == 79.0


def test_mos_forecast_us_list(monkeypatch):
    _mock_urlopen(monkeypatch, '[{"ftime":"2026-07-05T00:00","n_x":96.0}]')
    rows = iem.mos_forecast("KLGA", datetime(2026, 7, 3), "MEX")
    assert rows and rows[0]["n_x"] == 96.0


def test_mos_forecast_intl_empty(monkeypatch):
    _mock_urlopen(monkeypatch, "")  # IEM returns header-only/blank for intl
    assert iem.mos_forecast("VHHH", datetime(2026, 7, 3)) == []


def test_unit_helpers():
    assert iem.f_to_c(212) == pytest.approx(100.0)
    assert iem.c_to_f(0) == pytest.approx(32.0)
    assert iem.f_to_c(97.0) == pytest.approx(36.11, abs=0.01)
