"""Async wrapper around the synchronous weather bot scanning pipeline."""

from __future__ import annotations

import asyncio
import sys
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

# Ensure the project root is on sys.path so weather/ imports work
_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


class ScanResult(NamedTuple):
    signals: list[dict]
    stats: dict
    active_cities: list[dict]
    scan_time: str


def _signal_to_dict(s) -> dict:
    loc = s.market.location
    return {
        "market_id": s.market.market_id,
        "title": s.market.title,
        "city": loc.city,
        "country": loc.country,
        "lat": loc.lat,
        "lon": loc.lon,
        "metric": s.market.metric,
        "threshold": s.market.threshold,
        "threshold_high": s.market.threshold_high,
        "direction_market": s.market.direction,
        "market_p": round(s.market_p, 4),
        "model_p": round(s.model_p, 4),
        "edge_pp": round(s.edge_pp, 4),
        "direction": s.direction,
        "ensemble_spread": round(s.ensemble_spread, 4),
        "confidence_score": round(s.confidence_score, 4),
        "quality_gate_passed": s.quality_gate_passed,
        "rejection_reason": s.rejection_reason,
        "signal_time": s.signal_time.isoformat(),
        "resolution_date": s.market.resolution_date.isoformat() if s.market.resolution_date else None,
        "url": s.market.url,
        "liquidity_usd": s.market.liquidity_usd,
        "n_members": s.forecast.n_members,
        "model_breakdown": {k: round(v, 4) for k, v in s.prob_result.model_breakdown.items()},
    }


def _stats_to_dict(stats) -> dict:
    return {
        "total_trades": stats.total_trades,
        "resolved_trades": stats.resolved_trades,
        "win_rate": stats.win_rate,
        "profit_factor": stats.profit_factor,
        "mean_brier_score": stats.mean_brier_score,
        "brier_skill_score": stats.brier_skill_score,
        "total_paper_pnl": stats.total_paper_pnl,
        "avg_edge_pp": stats.avg_edge_pp,
        "max_drawdown_pct": stats.max_drawdown_pct,
        "ready_for_live": stats.ready_for_live,
        "failure_reasons": stats.failure_reasons,
    }


def _run_scan_sync(log_paper: bool = True) -> ScanResult:
    from dotenv import load_dotenv
    load_dotenv(_PROJECT_ROOT / ".env")

    from weather.market_scanner import WeatherMarketScanner
    from weather.probability_model import ProbabilityModel
    from weather.signal_generator import SignalGenerator
    from weather.paper_trader import PaperTrader
    from weather.weather_client import WeatherClient

    scanner = WeatherMarketScanner()
    client = WeatherClient()
    model = ProbabilityModel()
    generator = SignalGenerator(model=model, client=client)
    paper = PaperTrader() if log_paper else None

    markets = scanner.scan()
    signals_raw = [generator.evaluate(m) for m in markets]

    if paper:
        for s in signals_raw:
            if s.quality_gate_passed:
                paper.log_trade(s)

    stats = (paper.compute_stats() if paper else PaperTrader().compute_stats())

    signals = [_signal_to_dict(s) for s in signals_raw]
    active_cities = [
        {
            "city": s["city"],
            "lat": s["lat"],
            "lon": s["lon"],
            "edge_pp": s["edge_pp"],
            "direction": s["direction"],
            "confidence_score": s["confidence_score"],
        }
        for s in signals
        if s["quality_gate_passed"]
    ]

    return ScanResult(
        signals=signals,
        stats=_stats_to_dict(stats),
        active_cities=active_cities,
        scan_time=datetime.now(timezone.utc).isoformat(),
    )


async def run_scan_async(log_paper: bool = True) -> ScanResult:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _run_scan_sync, log_paper)
