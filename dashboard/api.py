"""REST API endpoints for the weather bot dashboard."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

from .state import bot_state
from .scanner_bridge import run_scan_async

router = APIRouter()

TRADES_LOG = Path(__file__).parent.parent / "logs" / "paper_trades.csv"


class IntervalBody(BaseModel):
    seconds: int


@router.get("/status")
async def get_status():
    return {
        "is_scanning": bot_state.is_scanning,
        "scan_interval": bot_state.scan_interval,
        "scan_count": bot_state.scan_count,
        "last_scan_at": bot_state.last_scan_at,
        "last_error": bot_state.last_error,
    }


@router.get("/signals")
async def get_signals():
    return bot_state.latest_signals


@router.get("/stats")
async def get_stats():
    return bot_state.stats or {}


@router.get("/trades")
async def get_trades():
    if not TRADES_LOG.exists():
        return []
    with open(TRADES_LOG) as f:
        rows = list(csv.DictReader(f))
    return rows[-50:]


@router.post("/scan")
async def trigger_scan(background_tasks: BackgroundTasks):
    """Fire a one-shot scan immediately."""
    background_tasks.add_task(_do_scan, log_paper=True)
    return {"status": "scan_started"}


@router.post("/start")
async def start_loop():
    """Start the continuous paper trading scan loop."""
    if bot_state.is_scanning:
        return {"status": "already_running"}
    import asyncio
    bot_state.scan_task = asyncio.create_task(_scan_loop())
    bot_state.is_scanning = True
    await bot_state.result_queue.put({"type": "status", "is_scanning": True})
    return {"status": "started"}


@router.post("/stop")
async def stop_loop():
    """Stop the continuous scan loop."""
    if bot_state.scan_task and not bot_state.scan_task.done():
        bot_state.scan_task.cancel()
    bot_state.is_scanning = False
    bot_state.scan_task = None
    await bot_state.result_queue.put({"type": "status", "is_scanning": False})
    return {"status": "stopped"}


@router.post("/interval")
async def set_interval(body: IntervalBody):
    if body.seconds < 60:
        raise HTTPException(400, "Minimum interval is 60 seconds")
    bot_state.scan_interval = body.seconds
    return {"status": "ok", "scan_interval": body.seconds}


async def _do_scan(log_paper: bool = True):
    import asyncio
    bot_state.scan_count += 1
    n = bot_state.scan_count
    await bot_state.result_queue.put({"type": "scan_started", "scan_count": n})
    try:
        result = await run_scan_async(log_paper=log_paper)
        bot_state.latest_signals = result.signals
        bot_state.stats = result.stats
        bot_state.active_cities = result.active_cities
        bot_state.last_scan_at = result.scan_time
        bot_state.last_error = None
        await bot_state.result_queue.put({
            "type": "scan_result",
            "signals": result.signals,
            "stats": result.stats,
            "active_cities": result.active_cities,
            "scan_count": n,
            "scan_time": result.scan_time,
        })
    except Exception as e:
        bot_state.last_error = str(e)
        await bot_state.result_queue.put({"type": "scan_error", "message": str(e)})


async def _scan_loop():
    import asyncio
    while True:
        await _do_scan(log_paper=True)
        await asyncio.sleep(bot_state.scan_interval)
