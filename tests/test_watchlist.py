"""Tests for the intraday watchlist hand-off (I1)."""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from weather.models import Location, WeatherMarket
from weather.watchlist import load_watchlist, refresh_from_books, save_watchlist


def _market(**kw):
    m = WeatherMarket(
        market_id="0xabc",
        title="Highest temperature in NYC on July 8?",
        yes_price=0.42,
        liquidity_usd=500.0,
        resolution_date=datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc),
        resolution_source="Weather Underground",
        location=Location(city="NYC", lat=40.7794, lon=-73.8803,
                          timezone="America/New_York", country="US"),
        metric="temperature_2m_max",
        threshold=35.0,
        direction="range",
        threshold_high=35.56,
        url="https://polymarket.com/x",
        yes_token_id="ytok", no_token_id="ntok",
        station_icao="KLGA", station_country="US", resolve_unit="F",
    )
    for k, v in kw.items():
        setattr(m, k, v)
    return m


class TestRoundTrip:
    def test_save_load_preserves_market(self, tmp_path):
        p = tmp_path / "watchlist.json"
        n = save_watchlist([_market()], p)
        assert n == 1
        loaded, age = load_watchlist(p)
        assert age < 5
        wm = loaded[0]
        assert wm.market_id == "0xabc"
        assert wm.station_icao == "KLGA" and wm.resolve_unit == "F"
        assert wm.location.timezone == "America/New_York"
        assert wm.resolution_date == datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
        assert wm.threshold_high == pytest.approx(35.56)

    def test_missing_file_returns_empty_and_stale(self, tmp_path):
        loaded, age = load_watchlist(tmp_path / "nope.json")
        assert loaded == [] and age == float("inf")

    def test_corrupt_file_returns_empty(self, tmp_path):
        p = tmp_path / "watchlist.json"
        p.write_text("{not json")
        loaded, age = load_watchlist(p)
        assert loaded == [] and age == float("inf")


class TestRefreshFromBooks:
    def _scanner(self, yes_summary, no_summary):
        sc = MagicMock()
        sc._fetch_book_summary.side_effect = lambda tok: (
            yes_summary if tok == "ytok" else no_summary)
        return sc

    def test_quote_becomes_book_mid(self):
        wm = _market()
        sc = self._scanner(
            {"best_ask": 0.46, "best_bid": 0.42, "depth_usd": 900.0},
            {"best_ask": 0.57, "best_bid": 0.55, "depth_usd": 700.0},
        )
        assert refresh_from_books(sc, wm) is True
        assert wm.yes_price == pytest.approx(0.44)   # mid of the YES book
        assert wm.no_best_ask == pytest.approx(0.57)
        assert wm.no_book_depth_usd == pytest.approx(700.0)

    def test_unusable_book_skips_market(self):
        wm = _market()
        sc = self._scanner({}, {})   # no quotes at all
        assert refresh_from_books(sc, wm) is False
        assert wm.yes_price == pytest.approx(0.42)   # untouched


class TestScanSourceColumn:
    def test_intraday_trades_are_attributed(self, tmp_path):
        from weather.paper_trader import PaperTrader
        from tests.test_signal_generator import _make_generator, _make_market

        gen = _make_generator(model_p=0.10)
        sig = gen.evaluate(_make_market(yes_price=0.60))
        assert sig.quality_gate_passed
        paper = PaperTrader(log_path=tmp_path / "paper_trades.csv")
        t = paper.log_trade(sig, scan_source="intraday")
        assert t is not None and t.scan_source == "intraday"
        import csv
        row = list(csv.DictReader(open(tmp_path / "paper_trades.csv")))[0]
        assert row["scan_source"] == "intraday"

    def test_default_is_hourly(self, tmp_path):
        from weather.paper_trader import PaperTrader
        from tests.test_signal_generator import _make_generator, _make_market
        gen = _make_generator(model_p=0.10)
        sig = gen.evaluate(_make_market(yes_price=0.60))
        paper = PaperTrader(log_path=tmp_path / "paper_trades.csv")
        t = paper.log_trade(sig)
        assert t.scan_source == "hourly"
