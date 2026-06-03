"""
Price history tracker for Gate 6 (odds velocity / informed flow detection).

Records yes_price per market at each evaluation cycle and computes the rate
of change over a rolling window. Fast price movement suggests informed flow —
someone is trading on information the model doesn't have.

In-memory cache avoids scanning the full CSV on every Gate 6 call. The file
is append-only and serves as a persistence layer across restarts; on init,
only recent rows (within window + 1h buffer) are loaded into the cache.
"""
from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import VELOCITY_WINDOW_HOURS


PRICE_HISTORY_LOG = Path("logs/price_history.csv")
_CSV_HEADERS = ["market_id", "yes_price", "recorded_at"]
_CACHE_BUFFER_HOURS = 1  # extra buffer beyond window kept in cache


class PriceTracker:
    def __init__(self, log_path: Path = PRICE_HISTORY_LOG):
        self.log_path = log_path
        self._cache: dict[str, list[dict]] = {}
        self._load_recent_into_cache()

    def record(self, market_id: str, yes_price: float) -> None:
        """Append a price snapshot and update the in-memory cache."""
        now = datetime.now(timezone.utc)
        entry = {"price": round(yes_price, 4), "ts": now}
        bucket = self._cache.setdefault(market_id, [])
        bucket.append(entry)

        # Prune entries older than max window + buffer to bound memory
        cutoff = now - timedelta(hours=VELOCITY_WINDOW_HOURS + _CACHE_BUFFER_HOURS)
        self._cache[market_id] = [r for r in bucket if r["ts"] >= cutoff]

        self.log_path.parent.mkdir(exist_ok=True)
        # Append-only: each write is a single fwrite() call, which POSIX guarantees
        # is atomic for pipes and regular files when the data fits in PIPE_BUF. A CSV
        # row is well under that limit. No tmp+replace needed here.
        with open(self.log_path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=_CSV_HEADERS)
            if f.tell() == 0:
                writer.writeheader()
            writer.writerow({
                "market_id": market_id,
                "yes_price": entry["price"],
                "recorded_at": now.isoformat(),
            })

    def get_velocity(self, market_id: str, window_hours: float) -> float | None:
        """
        Price delta (newest - oldest) over the rolling window.

        Returns None if fewer than 2 data points exist in the window.
        Positive = price moved up; negative = price moved down.
        """
        rows = self._cache.get(market_id, [])
        if len(rows) < 2:
            return None

        cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
        window_rows = [r for r in rows if r["ts"] >= cutoff]
        if len(window_rows) < 2:
            return None

        return round(window_rows[-1]["price"] - window_rows[0]["price"], 4)

    def _load_recent_into_cache(self) -> None:
        """Populate cache from file on startup, skipping rows outside the window."""
        if not self.log_path.exists():
            return
        cutoff = datetime.now(timezone.utc) - timedelta(
            hours=VELOCITY_WINDOW_HOURS + _CACHE_BUFFER_HOURS
        )
        with open(self.log_path) as f:
            for row in csv.DictReader(f):
                try:
                    ts = datetime.fromisoformat(row["recorded_at"])
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    if ts < cutoff:
                        continue
                    mid = row["market_id"]
                    self._cache.setdefault(mid, []).append(
                        {"price": float(row["yes_price"]), "ts": ts}
                    )
                except (ValueError, KeyError):
                    continue
        for rows in self._cache.values():
            rows.sort(key=lambda r: r["ts"])
