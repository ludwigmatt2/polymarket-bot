"""Tests for telegram_views — pure formatters for /why, /scanreport, /losses."""

import json

import telegram_views
from telegram_views import (
    MAX_MSG_CHARS,
    find_trade,
    fmt_losses,
    fmt_scanreport,
    fmt_why,
)


def _row(**overrides) -> dict:
    """A resolved NO trade row mirroring the production CSV schema."""
    base = {
        "trade_id": "abc12345",
        "market_id": "mkt-1",
        "market_title": "Highest temperature in Toronto on June 12? - Will it reach 25?",
        "signal_time": "2026-06-11T09:00:00+00:00",
        "entry_price": "0.725",
        "model_p": "0.156",
        "direction": "NO",
        "size_usd": "15.53",
        "edge_pp": "0.1182",
        "ensemble_spread": "0.0749",
        "confidence_score": "0.61",
        "resolution_date": "2026-06-12",
        "metric": "temperature_2m_max",
        "threshold": "25.0",
        "threshold_high": "",
        "weather_direction": "above",
        "actual_outcome": "1",
        "resolved_at": "2026-06-13T12:00:00+00:00",
        "pnl_usd": "-15.53",
        "brier_score": "0.7123",
        "raw_p": "0.0848",
        "model_breakdown_json": json.dumps({"gfs_seamless": 0.10, "ecmwf_ifs025": 0.07}),
    }
    base.update(overrides)
    return base


class TestFmtWhy:
    def test_contains_model_chain_and_outcome(self):
        text = fmt_why(_row())
        assert "8.5%" in text          # raw_p
        assert "15.6%" in text         # model_p after shrink
        assert "27.5%" in text         # yes_price = 1 - 0.725 entry for NO
        assert "gfs_seamless" in text
        assert "Resolved YES" in text
        assert "-15.53" in text

    def test_yes_direction_uses_entry_as_yes_price(self):
        text = fmt_why(_row(direction="YES", entry_price="0.30"))
        assert "30.0%" in text

    def test_unresolved_trade_has_no_outcome_block(self):
        text = fmt_why(_row(actual_outcome="", resolved_at="", pnl_usd="", brier_score=""))
        assert "Resolved" not in text

    def test_missing_optional_fields_dont_crash(self):
        text = fmt_why(_row(raw_p="", model_breakdown_json="", ensemble_spread="",
                            confidence_score=""))
        assert "Why this trade" in text

    def test_range_market_condition(self):
        text = fmt_why(_row(weather_direction="range", threshold="20", threshold_high="22"))
        assert "between 20 and 22" in text


class TestFindTrade:
    def test_exact_and_prefix_match(self):
        rows = [_row(trade_id="abc12345"), _row(trade_id="def67890")]
        assert find_trade(rows, "abc12345")["trade_id"] == "abc12345"
        assert find_trade(rows, "def")["trade_id"] == "def67890"

    def test_ambiguous_prefix_returns_none(self):
        rows = [_row(trade_id="abc11111"), _row(trade_id="abc22222")]
        assert find_trade(rows, "abc") is None

    def test_no_match_returns_none(self):
        assert find_trade([_row()], "zzz") is None


class TestFmtScanreport:
    def test_renders_funnel(self):
        data = {
            "scanned_at": "2026-06-12T09:00:00",
            "funnel": {
                "fetched": 773, "parsed": 695, "unparseable": 78, "tradeable": 120,
                "evaluated": 120, "actionable": 25,
                "rejections": {"gate4": 40, "gate8": 30, "gate2.5": 25},
                "top_rejected": [
                    {"title": "X - Will it rain?", "reason": "gate4: edge below fees", "edge_pp": 0.03},
                ],
            },
        }
        text = fmt_scanreport(data)
        assert "773" in text and "695" in text and "25" in text
        assert "gate4: 40" in text
        assert "edge below fees" in text

    def test_no_funnel_yet(self):
        assert "No funnel data" in fmt_scanreport({"signals": []})

    def test_truncates_at_telegram_limit(self):
        data = {
            "scanned_at": "2026-06-12T09:00:00",
            "funnel": {
                "fetched": 1, "parsed": 1, "unparseable": 0, "tradeable": 1,
                "evaluated": 1, "actionable": 0,
                "rejections": {f"gate{i}": i for i in range(200)},
                "top_rejected": [
                    {"title": "T" * 200, "reason": "R" * 200, "edge_pp": 0.5}
                ] * 50,
            },
        }
        assert len(fmt_scanreport(data)) <= MAX_MSG_CHARS


class TestFmtLosses:
    def test_sorted_by_loss_with_cause(self):
        rows = [
            _row(trade_id="t1", pnl_usd="-5.00"),
            _row(trade_id="t2", pnl_usd="-20.00"),
            _row(trade_id="t3", pnl_usd="10.00"),   # winner — excluded
            _row(trade_id="t4", pnl_usd="", resolved_at=""),  # open — excluded
        ]
        text = fmt_losses(rows)
        assert "2 losing trades" in text
        assert text.index("-20.00") < text.index("-5.00")
        assert "high temp > 25.0" in text
        assert "model said 16% YES" in text
        assert "/why t2" in text

    def test_no_losses(self):
        assert "No losing trades" in fmt_losses([_row(pnl_usd="10.0")])

    def test_truncates_at_telegram_limit(self):
        rows = [
            _row(trade_id=f"t{i}", pnl_usd=f"-{i + 1}.00",
                 market_title="Very long market title " * 5)
            for i in range(30)
        ]
        assert len(fmt_losses(rows, n=30)) <= MAX_MSG_CHARS
