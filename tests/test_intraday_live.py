"""Live execution on the intraday track (Aug 23 2026) — mirrors run_scan()'s
live path exactly, but the one thing that's easy to get wrong porting it over
is the paper-mirror tag: execute_signal() defaults scan_source to "hourly",
so a live intraday fill that doesn't pass scan_source="intraday" explicitly
would silently corrupt the mainline_hourly/mainline_intraday split the
moment intraday goes live — right when watching it separately matters most."""
import csv
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import weather_bot
from weather.models import Location, Signal, WeatherMarket


def _market(market_id: str = "mkt_intraday_1") -> WeatherMarket:
    return WeatherMarket(
        market_id=market_id,
        title=f"Test intraday market {market_id}",
        yes_price=0.5,
        liquidity_usd=5000.0,
        resolution_date=datetime.now(timezone.utc) + timedelta(hours=2),
        resolution_source="Weather Underground",
        location=Location(city="Seoul", lat=37.4667, lon=126.45, timezone="Asia/Seoul"),
        metric="temperature_2m_min",
        threshold=26.0,
        direction="equal",
        url="https://polymarket.com/test",
        yes_token_id="yes_tok", no_token_id="no_tok",
        yes_best_bid=0.4, no_best_bid=0.55,
    )


def _signal(market: WeatherMarket, *, quality_gate_passed: bool = True) -> Signal:
    return Signal(
        market=market, model_p=0.65, market_p=0.5, edge_pp=0.15, direction="NO",
        ensemble_spread=0.05, confidence_score=0.80, size_factor=1.0,
        quality_gate_passed=quality_gate_passed, rejection_reason=None,
        signal_time=datetime.now(timezone.utc), forecast=MagicMock(),
        prob_result=MagicMock(model_breakdown={}),
    )


class FakeLiveTrader:
    def __init__(self, fill: bool = True):
        self.fill = fill
        self.calls: list[dict] = []
        self.reset_calls = 0

    def reset_scan_commitments(self):
        self.reset_calls += 1

    def execute_signal(self, signal, scan_source="hourly"):
        self.calls.append({"scan_source": scan_source})
        if not self.fill:
            return {"filled": 0.0, "size_usd": 0.0, "price": 0.0, "order_id": ""}
        return {"filled": 5.0, "size_usd": 2.5, "price": 0.5, "order_id": "0xabc123def"}


def _setup(monkeypatch, tmp_path, market, *, geoblocked: bool = False):
    from weather import station_obs as so_mod
    from weather import watchlist as wl_mod

    monkeypatch.setattr(wl_mod, "load_watchlist", lambda path: ([market], 10.0))
    monkeypatch.setattr(wl_mod, "apply_book_mid", lambda m: True)
    monkeypatch.setattr(so_mod, "is_event_day", lambda *a, **k: True)
    monkeypatch.setattr(weather_bot, "check_geoblock",
                         lambda: {"blocked": True} if geoblocked else None)

    scanner = MagicMock()
    scanner._fetch_books_bulk = MagicMock()
    return scanner


class TestIntradayLiveExecution:
    def test_live_fill_tags_paper_mirror_as_intraday_not_hourly(self, tmp_path, monkeypatch):
        market = _market()
        scanner = _setup(monkeypatch, tmp_path, market)
        generator = MagicMock()
        generator.evaluate.return_value = _signal(market)
        trader = FakeLiveTrader(fill=True)

        weather_bot.mode_intraday(scanner, generator, tmp_path, live_trader=trader)

        assert trader.calls == [{"scan_source": "intraday"}]
        assert trader.reset_calls == 1  # fresh in-tick spend budget

    def test_geoblocked_skips_execution_entirely(self, tmp_path, monkeypatch, capsys):
        market = _market()
        scanner = _setup(monkeypatch, tmp_path, market, geoblocked=True)
        generator = MagicMock()
        generator.evaluate.return_value = _signal(market)
        trader = FakeLiveTrader(fill=True)

        weather_bot.mode_intraday(scanner, generator, tmp_path, live_trader=trader)

        assert trader.calls == []  # never even attempted
        assert "ORDER_ISSUE" in capsys.readouterr().err

    def test_order_exception_tagged_order_issue_not_silently_swallowed(self, tmp_path, monkeypatch, capsys):
        market = _market()
        scanner = _setup(monkeypatch, tmp_path, market)
        generator = MagicMock()
        generator.evaluate.return_value = _signal(market)

        class BlowsUpTrader(FakeLiveTrader):
            def execute_signal(self, signal, scan_source="hourly"):
                raise ValueError("bad signature")

        weather_bot.mode_intraday(scanner, generator, tmp_path, live_trader=BlowsUpTrader())

        err = capsys.readouterr().err
        assert "ORDER_ISSUE" in err and "bad signature" in err

    def test_paper_mode_unaffected_when_no_live_trader(self, tmp_path, monkeypatch):
        """Regression guard: live_trader=None must behave exactly as before —
        paper-log the signal, no execute_signal call anywhere."""
        market = _market()
        scanner = _setup(monkeypatch, tmp_path, market)
        generator = MagicMock()
        generator.evaluate.return_value = _signal(market)

        weather_bot.mode_intraday(scanner, generator, tmp_path, live_trader=None)

        rows = list(csv.DictReader(open(tmp_path / "paper_trades.csv")))
        assert len(rows) == 1
        assert rows[0]["scan_source"] == "intraday"

    def test_low_quality_signal_neither_trades_live_nor_paper(self, tmp_path, monkeypatch):
        market = _market()
        scanner = _setup(monkeypatch, tmp_path, market)
        generator = MagicMock()
        generator.evaluate.return_value = _signal(market, quality_gate_passed=False)
        trader = FakeLiveTrader(fill=True)

        weather_bot.mode_intraday(scanner, generator, tmp_path, live_trader=trader)

        assert trader.calls == []
