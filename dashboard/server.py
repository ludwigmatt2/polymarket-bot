#!/usr/bin/env python3
"""
Weather Bot Dashboard — FastAPI server.

Run:
  python dashboard/server.py
  → opens browser at http://localhost:8765
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Ensure project root on path before any weather/ imports
_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

import asyncio
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from .api import router as api_router
from .state import bot_state

app = FastAPI(title="Weather Bot Dashboard")

# REST API
app.include_router(api_router, prefix="/api")

# Static files
_STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")


@app.get("/")
async def serve_index():
    return FileResponse(str(_STATIC / "index.html"))


# WebSocket hub
_connected: set[WebSocket] = set()


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    _connected.add(ws)
    # Send current state immediately on connect
    await ws.send_json({
        "type": "status",
        "is_scanning": bot_state.is_scanning,
        "scan_interval": bot_state.scan_interval,
        "scan_count": bot_state.scan_count,
        "last_scan_at": bot_state.last_scan_at,
    })
    if bot_state.latest_signals:
        await ws.send_json({
            "type": "scan_result",
            "signals": bot_state.latest_signals,
            "stats": bot_state.stats,
            "active_cities": bot_state.active_cities,
            "scan_count": bot_state.scan_count,
            "scan_time": bot_state.last_scan_at,
        })
    try:
        while True:
            await ws.receive_text()  # keep alive; client doesn't send commands
    except WebSocketDisconnect:
        _connected.discard(ws)


@app.on_event("startup")
async def startup():
    asyncio.create_task(_broadcast_loop())


async def _broadcast_loop():
    """Drain the result queue and broadcast to all connected WebSocket clients."""
    while True:
        msg = await bot_state.result_queue.get()
        dead = set()
        for ws in list(_connected):
            try:
                await ws.send_json(msg)
            except Exception:
                dead.add(ws)
        _connected -= dead


if __name__ == "__main__":
    import threading
    import webbrowser
    import uvicorn

    def _open_browser():
        webbrowser.open("http://localhost:8765")

    threading.Timer(1.5, _open_browser).start()
    uvicorn.run(
        "dashboard.server:app",
        host="127.0.0.1",
        port=8765,
        reload=False,
        log_level="info",
    )
