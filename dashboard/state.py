"""Shared in-memory state for the dashboard server."""

from __future__ import annotations

import asyncio
from typing import Any


class BotState:
    def __init__(self):
        self.is_scanning: bool = False
        self.scan_interval: int = 3600
        self.scan_count: int = 0
        self.last_scan_at: str | None = None
        self.latest_signals: list[dict] = []
        self.stats: dict | None = None
        self.active_cities: list[dict] = []
        self.scan_task: asyncio.Task | None = None
        self.result_queue: asyncio.Queue = asyncio.Queue()
        self.last_error: str | None = None


bot_state = BotState()
