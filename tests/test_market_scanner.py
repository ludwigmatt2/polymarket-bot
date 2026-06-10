"""Tests for WeatherMarketScanner — book-depth (B4), parser cross-check (E4), zero-markets alarm (E3)."""

from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from weather.market_scanner import WeatherMarketScanner, _GammaMarket, _MISMATCH_DROP, SCANNER_ALARM_LOG
from weather.models import WeatherMarket


def _make_gamma_market(
    condition_id="abc123",
    title="Temperature in London on June 1? - Will the high be above 20°C on June 1?",
    description="This market resolves YES if the maximum temperature in London on June 1, 2026 is above 20°C.",
    yes_price=0.55,
    liquidity=500.0,
    days_out=2,
) -> _GammaMarket:
    end_date = (datetime.now(timezone.utc) + timedelta(days=days_out)).isoformat()
    market = {
        "conditionId": condition_id,
        "outcomePrices": f'[{yes_price}, {1 - yes_price}]',
        "volumeClob": str(liquidity),
        "question": "Will the high be above 20°C on June 1?",
        "description": description,
        "endDate": end_date,
    }
    event = {
        "title": "Temperature in London on June 1?",
        "slug": "temperature-london-june-1",
        "description": description,
    }
    return _GammaMarket(market, event)


def _make_scanner_with_geocode(lat=51.5, lon=-0.12, tz="Europe/London") -> WeatherMarketScanner:
    """Return a scanner whose pmxt and geocode calls are mocked."""
    loc = MagicMock()
    loc.city = "London"
    loc.lat = lat
    loc.lon = lon
    loc.timezone = tz
    mock_poly = MagicMock()
    mock_poly.fetch_market.side_effect = Exception("sidecar not running")
    mock_wc = MagicMock()
    mock_wc.geocode.return_value = loc
    scanner = WeatherMarketScanner(poly=mock_poly, weather_client=mock_wc)
    return scanner


class TestBookDepthAttached:
    def test_book_depth_zero_when_sidecar_unavailable(self):
        """_parse_market leaves book_depth_usd=0.0; scan() populates it post-filter."""
        scanner = _make_scanner_with_geocode()
        scanner._poly.fetch_market.side_effect = Exception("sidecar not running")
        gm = _make_gamma_market()
        # _parse_market itself no longer calls _fetch_book_depth_usd
        wm = scanner._parse_market(gm)
        assert wm is not None
        assert wm.book_depth_usd == 0.0  # default; scan() would try to set it

    def test_fetch_book_depth_returns_zero_when_sidecar_unavailable(self):
        scanner = _make_scanner_with_geocode()
        scanner._poly.fetch_market.side_effect = Exception("sidecar not running")
        depth = scanner._fetch_book_depth_usd("abc123", 0.55)
        assert depth == 0.0

    def test_fetch_book_depth_computes_ask_side_usd(self):
        scanner = _make_scanner_with_geocode()
        mock_market = MagicMock()
        mock_yes = MagicMock()
        mock_yes.outcome_id = "yes-token-123"
        mock_market.yes = mock_yes
        scanner._poly.fetch_market.return_value = mock_market
        scanner._poly.fetch_market.side_effect = None

        mock_ob = MagicMock()
        mock_ob.asks = [MagicMock(price=0.55, size=100), MagicMock(price=0.57, size=50)]
        scanner._poly.fetch_order_book.return_value = mock_ob
        scanner._poly.fetch_order_book.side_effect = None

        depth = scanner._fetch_book_depth_usd("abc123", 0.55)
        # depth = 0.55*100 + 0.57*50 = 55 + 28.5 = 83.5
        assert abs(depth - 83.5) < 0.01

    def test_fetch_book_depth_zero_when_no_yes_outcome(self):
        scanner = _make_scanner_with_geocode()
        mock_market = MagicMock()
        mock_market.yes = None
        mock_market.outcomes = []
        scanner._poly.fetch_market.return_value = mock_market
        scanner._poly.fetch_market.side_effect = None
        depth = scanner._fetch_book_depth_usd("abc123", 0.55)
        assert depth == 0.0


class TestDescriptionCrossCheck:
    def test_agreeing_description_passes(self):
        scanner = _make_scanner_with_geocode()
        gm = _make_gamma_market(
            description="Resolves YES if the maximum temperature in London on June 1, 2026 exceeds 20°C."
        )
        wm = scanner._parse_market(gm)
        assert wm is not None

    def test_direction_conflict_drops_market(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "logs").mkdir()
        scanner = _make_scanner_with_geocode()
        # Title says "above 20°C" but description says "below"
        gm = _make_gamma_market(
            title="Temperature in London on June 1? - Will the high be above 20°C on June 1?",
            description="Resolves YES if the maximum temperature is below 20°C.",
        )
        result = scanner._parse_market(gm)
        assert result is _MISMATCH_DROP
        assert (tmp_path / "logs" / "parser_mismatch.csv").exists()

    def test_threshold_conflict_drops_market(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "logs").mkdir()
        scanner = _make_scanner_with_geocode()
        # Title says 20°C but description says 35°C (more than 3°C apart)
        gm = _make_gamma_market(
            title="Temperature in London on June 1? - Will the high be above 20°C on June 1?",
            description="Resolves YES if the temperature is above 35°C on June 1.",
        )
        result = scanner._parse_market(gm)
        assert result is _MISMATCH_DROP

    def test_empty_description_passes(self):
        scanner = _make_scanner_with_geocode()
        gm = _make_gamma_market(description="")
        wm = scanner._parse_market(gm)
        assert wm is not None

    def test_description_without_threshold_passes(self):
        """Description with only narrative text (no recognizable numbers) should not block."""
        scanner = _make_scanner_with_geocode()
        gm = _make_gamma_market(
            description="This market resolves based on official London weather data."
        )
        wm = scanner._parse_market(gm)
        assert wm is not None

    def test_example_degree_value_in_boilerplate_passes(self):
        """Regression (Jun 2026 drought): Polymarket boilerplate contains an EXAMPLE
        value "(eg, 9.1°C)" that is not a threshold. E4 must not treat it as
        conflicting evidence — doing so dropped every daily temp market for 7 days."""
        scanner = _make_scanner_with_geocode()
        gm = _make_gamma_market(
            title="Highest temperature in London on June 10? - Will the highest temperature in London be 24°C or below on June 10?",
            description=(
                "This market will resolve to the temperature range that contains the "
                "highest temperature recorded in degrees Celsius on 10 Jun '26. The "
                "resolution source for this market measures temperatures in Celsius to "
                "one decimal place (eg, 9.1°C). Thus, this is the level of precision "
                "that will be used when resolving the market."
            ),
        )
        wm = scanner._parse_market(gm)
        assert wm is not None and wm is not _MISMATCH_DROP

    def test_example_with_eg_dots_stripped(self):
        """Variant spellings: (e.g. 9.1°C) and (example: 9.1°C) are also stripped."""
        scanner = _make_scanner_with_geocode()
        for variant in ("(e.g. 9.1°C)", "(e.g., 9.1°C)", "(example: 9.1°C)"):
            gm = _make_gamma_market(
                title="Highest temperature in London on June 10? - Will the highest temperature in London be 24°C or below on June 10?",
                description=f"Temperatures are measured to one decimal place {variant}.",
            )
            wm = scanner._parse_market(gm)
            assert wm is not None and wm is not _MISMATCH_DROP, variant


class TestZeroMarketsAlarm:
    """E3: zero-results scan must log a warning and write scanner_alarm.csv."""

    def test_scan_warns_on_zero_markets(self, tmp_path, monkeypatch, caplog):
        """When Gamma search returns 0 events, alarm is written to scanner_alarm.csv."""
        import logging
        monkeypatch.chdir(tmp_path)
        (tmp_path / "logs").mkdir()

        scanner = _make_scanner_with_geocode()
        # Patch _gamma_search_keywords to return empty list (simulates Gamma returning 0 events)
        monkeypatch.setattr(scanner, "_gamma_search_keywords", lambda: [])

        with caplog.at_level(logging.WARNING, logger="weather.market_scanner"):
            result = scanner._search_keywords()

        assert result == []
        # WARNING must have been logged
        assert any("zero" in r.message.lower() or "0 market" in r.message.lower()
                   for r in caplog.records)
        # Alarm CSV must exist
        alarm_path = tmp_path / "logs" / "scanner_alarm.csv"
        assert alarm_path.exists()
        content = alarm_path.read_text()
        assert "zero_markets_returned" in content

    def test_no_alarm_when_markets_found(self, tmp_path, monkeypatch, caplog):
        """No alarm file is written when markets are returned."""
        import logging
        monkeypatch.chdir(tmp_path)
        (tmp_path / "logs").mkdir()

        scanner = _make_scanner_with_geocode()
        fake_market = _make_gamma_market()
        monkeypatch.setattr(scanner, "_gamma_search_keywords", lambda: [fake_market])

        with caplog.at_level(logging.WARNING, logger="weather.market_scanner"):
            result = scanner._search_keywords()

        assert len(result) == 1
        alarm_path = tmp_path / "logs" / "scanner_alarm.csv"
        assert not alarm_path.exists()


class TestParseRateAlarm:
    """Parse-rate collapse alarm: the Jun 2026 E4 regression dropped 90% of markets
    silently for 7 days. A collapsed parsed/fetched ratio must write scanner_alarm.csv."""

    @staticmethod
    def _junk(i):
        gm = _make_gamma_market(
            condition_id=f"junk{i}",
            description="Resolves YES if the Lakers win.",
        )
        # _GammaMarket derives title from event+question; override to be unparseable
        gm.title = "Will the Lakers win the 2026 NBA Finals?"
        return gm

    def test_alarm_on_parse_collapse(self, tmp_path, monkeypatch, caplog):
        import logging
        monkeypatch.chdir(tmp_path)
        (tmp_path / "logs").mkdir()
        scanner = _make_scanner_with_geocode()
        monkeypatch.setattr(scanner, "_search_keywords",
                            lambda: [self._junk(i) for i in range(60)])
        with caplog.at_level(logging.WARNING, logger="weather.market_scanner"):
            scanner.scan()
        alarm_path = tmp_path / "logs" / "scanner_alarm.csv"
        assert alarm_path.exists()
        assert "low_parse_rate" in alarm_path.read_text()
        assert any("parse rate" in r.message.lower() for r in caplog.records)

    def test_no_alarm_below_min_fetch(self, tmp_path, monkeypatch):
        """Tiny fetches (off-hours, API hiccups) must not alarm on rate alone."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "logs").mkdir()
        scanner = _make_scanner_with_geocode()
        monkeypatch.setattr(scanner, "_search_keywords",
                            lambda: [self._junk(i) for i in range(10)])
        scanner.scan()
        assert not (tmp_path / "logs" / "scanner_alarm.csv").exists()

    def test_no_alarm_on_healthy_parse(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "logs").mkdir()
        scanner = _make_scanner_with_geocode()
        monkeypatch.setattr(scanner, "_search_keywords",
                            lambda: [_make_gamma_market(condition_id=f"ok{i}") for i in range(60)])
        scanner.scan()
        assert not (tmp_path / "logs" / "scanner_alarm.csv").exists()
