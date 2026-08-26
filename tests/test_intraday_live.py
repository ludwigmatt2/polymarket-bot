"""Live execution on the intraday track (Aug 23 2026), decoupled from the paper
track (Aug 2026): mode_intraday ALWAYS logs every actionable signal to the paper
track with scan_source='intraday' — that's the model record and the go-live gate
basis — and executes live SEPARATELY (real USDC → live_trades.csv). The two must
not be entangled: a blocked/thin-book live order must never stop the paper track
from recording the signal (that entanglement once let a geoblock silently freeze
the whole mainline paper track for days)."""
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
        self.calls = 0
        self.reset_calls = 0

    def reset_scan_commitments(self):
        self.reset_calls += 1

    def execute_signal(self, signal):
        self.calls += 1
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


def _paper_rows(tmp_path):
    return list(csv.DictReader(open(tmp_path / "paper_trades.csv")))


class TestIntradayLiveExecution:
    def test_paper_logged_as_intraday_and_live_executed(self, tmp_path, monkeypatch):
        market = _market()
        scanner = _setup(monkeypatch, tmp_path, market)
        generator = MagicMock()
        generator.evaluate.return_value = _signal(market)
        trader = FakeLiveTrader(fill=True)

        weather_bot.mode_intraday(scanner, generator, tmp_path, live_trader=trader)

        rows = _paper_rows(tmp_path)
        assert len(rows) == 1 and rows[0]["scan_source"] == "intraday"  # model track
        assert trader.calls == 1  # live executed separately
        assert trader.reset_calls == 1  # fresh in-tick spend budget

    def test_geoblock_skips_live_but_paper_track_still_records(self, tmp_path, monkeypatch, capsys):
        """The whole point of the decoupling: a geoblocked live order must NOT
        stop the paper/model track from logging the signal."""
        market = _market()
        scanner = _setup(monkeypatch, tmp_path, market, geoblocked=True)
        generator = MagicMock()
        generator.evaluate.return_value = _signal(market)
        trader = FakeLiveTrader(fill=True)

        weather_bot.mode_intraday(scanner, generator, tmp_path, live_trader=trader)

        assert trader.calls == 0  # live never attempted
        assert "ORDER_ISSUE" in capsys.readouterr().err
        rows = _paper_rows(tmp_path)
        assert len(rows) == 1 and rows[0]["scan_source"] == "intraday"  # still recorded

    def test_order_exception_tagged_order_issue_not_silently_swallowed(self, tmp_path, monkeypatch, capsys):
        market = _market()
        scanner = _setup(monkeypatch, tmp_path, market)
        generator = MagicMock()
        generator.evaluate.return_value = _signal(market)

        class BlowsUpTrader(FakeLiveTrader):
            def execute_signal(self, signal):
                raise ValueError("bad signature")

        weather_bot.mode_intraday(scanner, generator, tmp_path, live_trader=BlowsUpTrader())

        err = capsys.readouterr().err
        assert "ORDER_ISSUE" in err and "bad signature" in err
        # paper track still recorded the signal before the live order blew up
        assert len(_paper_rows(tmp_path)) == 1

    def test_paper_mode_unaffected_when_no_live_trader(self, tmp_path, monkeypatch):
        """live_trader=None must behave as before — paper-log the signal, no
        execute_signal call anywhere."""
        market = _market()
        scanner = _setup(monkeypatch, tmp_path, market)
        generator = MagicMock()
        generator.evaluate.return_value = _signal(market)

        weather_bot.mode_intraday(scanner, generator, tmp_path, live_trader=None)

        rows = _paper_rows(tmp_path)
        assert len(rows) == 1
        assert rows[0]["scan_source"] == "intraday"

    def test_low_quality_signal_neither_trades_live_nor_paper(self, tmp_path, monkeypatch):
        market = _market()
        scanner = _setup(monkeypatch, tmp_path, market)
        generator = MagicMock()
        generator.evaluate.return_value = _signal(market, quality_gate_passed=False)
        trader = FakeLiveTrader(fill=True)

        weather_bot.mode_intraday(scanner, generator, tmp_path, live_trader=trader)

        assert trader.calls == 0
        assert not (tmp_path / "paper_trades.csv").exists() or _paper_rows(tmp_path) == []


class TestLiveTraderConstructionNeverKillsScan:
    """Polymarket's CLOB auth backend intermittently 400s re-deriving API keys
    for deposit-wallet accounts ("Could not derive api key!", an upstream bug).
    Before this wrapper, that exception propagated out of _build_admin_live_trader
    uncaught, crashing the whole weather_bot.py subprocess — taking paper-track
    logging down with it for the entire cycle, not just live execution."""

    def test_credential_failure_degrades_to_none_not_crash(self, tmp_path, monkeypatch, capsys):
        def _boom(paper, log_dir, bankroll):
            raise RuntimeError("CLOB auth failed: PolyApiException[status_code=400, "
                                "error_message={'error': 'Could not derive api key!'}]")
        monkeypatch.setattr(weather_bot, "_build_admin_live_trader", _boom)

        result = weather_bot._build_live_trader_or_none(MagicMock(), tmp_path, 500.0)

        assert result is None
        err = capsys.readouterr().err
        assert "ORDER_ISSUE" in err and "Could not derive api key" in err

    def test_sys_exit_from_gate_or_missing_creds_also_degrades_to_none(self, tmp_path, monkeypatch, capsys):
        """_build_admin_live_trader intentionally sys.exit(1)s on a closed gate
        or missing admin creds — that must not kill an unattended scheduled scan
        either."""
        def _exits(paper, log_dir, bankroll):
            import sys as _sys
            _sys.exit(1)
        monkeypatch.setattr(weather_bot, "_build_admin_live_trader", _exits)

        result = weather_bot._build_live_trader_or_none(MagicMock(), tmp_path, 500.0)

        assert result is None
        assert "ORDER_ISSUE" in capsys.readouterr().err

    def test_success_path_passes_through_unchanged(self, tmp_path, monkeypatch):
        sentinel = FakeLiveTrader()
        monkeypatch.setattr(weather_bot, "_build_admin_live_trader",
                             lambda paper, log_dir, bankroll: sentinel)

        result = weather_bot._build_live_trader_or_none(MagicMock(), tmp_path, 500.0)

        assert result is sentinel
