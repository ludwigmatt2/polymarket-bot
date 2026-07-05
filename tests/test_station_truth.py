"""Tests for wu_client (Wunderground-direct) and station_truth (WU→IEM fallback)."""
import json
from datetime import date

import pytest

from weather import iem_client, station_parser, station_truth, wu_client

_MIAMI_DESC = (
    "This market will resolve to the temperature range that contains the highest "
    "temperature recorded at the Miami Intl Airport Station in degrees Fahrenheit "
    "on 4 Jul '26. The resolution source for this market will be information from "
    "Wunderground, specifically the highest temperature recorded for all times on "
    "this day by the Forecast for the Miami Intl Airport Station once information is "
    "finalized, available here: https://www.wunderground.com/history/daily/us/fl/miami/KMIA."
)
_SEOUL_DESC = (
    "This market will resolve to the temperature range that contains the highest "
    "temperature recorded at the Incheon Intl Airport Station in degrees Celsius on "
    "8 May '26. ... available here: "
    "https://www.wunderground.com/history/daily/kr/incheon/RKSI."
)


def test_parse_station_us():
    r = station_parser.station_from_description(_MIAMI_DESC)
    assert r == {"icao": "KMIA", "country": "US", "unit": "F"}


def test_parse_station_intl():
    r = station_parser.station_from_description(_SEOUL_DESC)
    assert r == {"icao": "RKSI", "country": "KR", "unit": "C"}


def test_parse_station_none():
    assert station_parser.station_from_description("no url here") is None
    assert station_parser.station_from_description(None) is None


class _Resp:
    def __init__(self, body): self._b = body.encode()
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return self._b


# ── wu_client ────────────────────────────────────────────────────────────────
def test_country_derivation():
    assert wu_client._country("KLGA") == "US"
    assert wu_client._country("VHHH") == "HK"
    assert wu_client._country("RKSI") == "KR"
    assert wu_client._country("ZZZZ") is None


def test_daily_high_low_parses(monkeypatch):
    body = json.dumps({"observations": [
        {"temp": 88}, {"temp": 91}, {"temp": None}, {"temp": 80}]})
    cap = {}
    def fake(req, timeout=20):
        cap["url"] = req.full_url
        return _Resp(body)
    monkeypatch.setattr("urllib.request.urlopen", fake)
    r = wu_client.daily_high_low("KMIA", date(2026, 7, 4))
    assert r == {"max_f": 91.0, "min_f": 80.0, "source": "wunderground"}
    assert "KMIA:9:US" in cap["url"] and "startDate=20260704" in cap["url"]


def test_daily_high_low_key_override(monkeypatch):
    monkeypatch.setenv("WU_API_KEY", "MYKEY")
    cap = {}
    def fake(req, timeout=20):
        cap["url"] = req.full_url
        return _Resp(json.dumps({"observations": [{"temp": 70}]}))
    monkeypatch.setattr("urllib.request.urlopen", fake)
    wu_client.daily_high_low("KLGA", date(2026, 7, 4))
    assert "apiKey=MYKEY" in cap["url"]


def test_daily_high_low_failure_returns_none(monkeypatch):
    def boom(req, timeout=20): raise OSError("blocked")
    monkeypatch.setattr("urllib.request.urlopen", boom)
    assert wu_client.daily_high_low("KLGA", date(2026, 7, 4)) is None


# ── station_truth fallback chain ─────────────────────────────────────────────
def test_prefers_wunderground(monkeypatch):
    monkeypatch.setattr(wu_client, "daily_high_low",
                        lambda i, d: {"max_f": 97.0, "min_f": 74.0, "source": "wunderground"})
    v, src = station_truth.daily_value_f("KLGA", date(2026, 7, 4), "temperature_2m_max")
    assert v == 97.0 and src == "wunderground"


def test_falls_back_to_iem_peak(monkeypatch):
    monkeypatch.setattr(wu_client, "daily_high_low", lambda i, d: None)
    monkeypatch.setattr(iem_client, "metar_peak", lambda i, d, kind: 91.0)
    v, src = station_truth.daily_value_f("KMIA", date(2026, 7, 4), "temperature_2m_max")
    assert v == 91.0 and src == "iem_metar_peak"


def test_falls_back_to_iem_dsm(monkeypatch):
    monkeypatch.setattr(wu_client, "daily_high_low", lambda i, d: None)
    monkeypatch.setattr(iem_client, "metar_peak", lambda i, d, kind: None)
    monkeypatch.setattr(iem_client, "daily_maxmin", lambda i, d: {"max_f": 93.0, "min_f": 75.0})
    v, src = station_truth.daily_value_f("KATL", date(2026, 7, 4), "temperature_2m_max")
    assert v == 93.0 and src == "iem_dsm"


def test_all_fail_returns_none(monkeypatch):
    monkeypatch.setattr(wu_client, "daily_high_low", lambda i, d: None)
    monkeypatch.setattr(iem_client, "metar_peak", lambda i, d, kind: None)
    monkeypatch.setattr(iem_client, "daily_maxmin", lambda i, d: None)
    assert station_truth.daily_value_f("KLGA", date(2026, 7, 4), "temperature_2m_max") == (None, None)


def test_unsupported_metric():
    assert station_truth.daily_value_f("KLGA", date(2026, 7, 4), "precipitation_sum") == (None, None)


def test_celsius_conversion(monkeypatch):
    monkeypatch.setattr(wu_client, "daily_high_low",
                        lambda i, d: {"max_f": 97.0, "min_f": 74.0, "source": "wunderground"})
    v, src = station_truth.daily_value_c("KLGA", date(2026, 7, 4), "temperature_2m_max")
    assert v == pytest.approx(36.11, abs=0.01) and src == "wunderground"
