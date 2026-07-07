"""Tests for the station relabel script's pure logic (no network)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from relabel_paper_truth import CITY_STATIONS, calibration_rows, station_for_row


class TestStationForRow:
    def test_legacy_row_maps_by_title(self):
        row = {"market_title": "Highest temperature in NYC on July 4? - Will the highest",
               "station_icao": "", "resolve_unit": ""}
        assert station_for_row(row) == ("KLGA", "US", "F")

    def test_title_unit_beats_city_default(self):
        # London defaults to °F in CITY_STATIONS, but a °C title must win
        row = {"market_title": "Will the highest temperature in London be 31°C on July 8?",
               "station_icao": "", "resolve_unit": ""}
        assert station_for_row(row) == ("EGLC", "GB", "C")

    def test_row_station_tag_preferred(self):
        row = {"market_title": "Highest temperature in Paris on July 6?",
               "station_icao": "LFPG", "station_country": "FR", "resolve_unit": "C"}
        icao, country, unit = station_for_row(row)
        assert icao == "LFPG" and unit == "C"

    def test_kdfw_remapped_to_kdal(self):
        # rows logged while the registry pointed Dallas at DFW
        row = {"market_title": "Highest temperature in Dallas on July 6?",
               "station_icao": "KDFW", "station_country": "US", "resolve_unit": "F"}
        assert station_for_row(row)[0] == "KDAL"

    def test_unmapped_city_returns_none(self):
        row = {"market_title": "Highest temperature in Madrid on July 8?",
               "station_icao": "", "resolve_unit": ""}
        assert station_for_row(row) is None

    def test_dallas_is_love_field(self):
        assert CITY_STATIONS["Dallas"][0] == "KDAL"


class TestCalibrationRows:
    def test_only_station_labeled_rows_with_raw_p(self):
        rows = [
            {"label_source": "station", "raw_p": "0.12", "actual_outcome": 1,
             "weather_direction": "range", "resolved_at": "2026-07-01T00:00:00"},
            {"label_source": "station", "raw_p": "", "actual_outcome": 0,
             "weather_direction": "equal", "resolved_at": ""},        # no raw_p → excluded
            {"label_source": "", "raw_p": "0.5", "actual_outcome": 1,
             "weather_direction": "equal", "resolved_at": ""},        # grid label → excluded
        ]
        out = calibration_rows(rows)
        assert len(out) == 1
        assert out[0]["model_p"] == 0.12 and out[0]["direction"] == "range"
