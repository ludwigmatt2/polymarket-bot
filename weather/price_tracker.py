"""
Price history tracker for Gate 6 (odds velocity / informed flow detection).

Records yes_price per market at each evaluation cycle and computes the rate
of change over a rolling window. Fast price movement suggests informed flow —
someone is trading on information the model doesn't have.
"""
from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path


PRICE_HISTORY_LOG = Path("logs/price_history.csv")
_CSV_HEADERS = ["market_id", "yes_price", "recorded_at"]


class PriceTracker:
    def __init__(self, log_path: Path = PRICE_HISTORY_LOG):
        self.log_path = log_path

    def record(self, market_id: str, yes_price: float) -> None:
        """Append a price snapshot. Called after every gate evaluation."""
        self.log_path.parent.mkdir(exist_ok=True)
        is_new = not self.log_path.exists()
        with open(self.log_path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=_CSV_HEADERS)
            if is_new:
                writer.writeheader()
            writer.writerow({
                "market_id": market_id,
                "yes_price": round(yes_price, 4),
                "recorded_at": datetime.now(timezone.utc).isoformat(),
            })

    def get_velocity(self, market_id: str, window_hours: float) -> float | None:
        """
        Price delta (newest - oldest) over the rolling window.

        Returns None if fewer than 2 data points exist in the window.
        Positive = price moved up; negative = price moved down.
        """
        rows = self._load_market(market_id)
        if len(rows) < 2:
            return None

        cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
        window_rows = [r for r in rows if r["ts"] >= cutoff]
        if len(window_rows) < 2:
            return None

        return round(window_rows[-1]["price"] - window_rows[0]["price"], 4)

    def _load_market(self, market_id: str) -> list[dict]:
        if not self.log_path.exists():
            return []
        rows = []
        with open(self.log_path) as f:
            for row in csv.DictReader(f):
                if row["market_id"] != market_id:
                    continue
                try:
                    ts = datetime.fromisoformat(row["recorded_at"])
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    rows.append({"price": float(row["yes_price"]), "ts": ts})
                except (ValueError, KeyError):
                    continue
        return sorted(rows, key=lambda r: r["ts"])
