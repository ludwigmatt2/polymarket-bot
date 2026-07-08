"""Intraday watchlist — the hand-off from the hourly full scan to the
15-minute intraday loop (I1).

The full scan is expensive (Gamma keyword sweep + parse + filters); the things
that actually change intraday are the station tape and the books. So the full
scan serializes its event-day station markets here, and the intraday loop
re-evaluates just those with fresh observations and fresh executable quotes —
no re-scan, no re-parse.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path

from .models import Location, WeatherMarket


def save_watchlist(markets: list[WeatherMarket], path: Path) -> int:
    """Serialize event-day markets for the intraday loop. Overwrites atomically
    enough for a single hourly writer + read-only consumers."""
    rows = []
    for wm in markets:
        d = asdict(wm)
        d["resolution_date"] = wm.resolution_date.isoformat()
        d["forecast_start_date"] = (wm.forecast_start_date.isoformat()
                                    if wm.forecast_start_date else None)
        rows.append(d)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"written_at": time.time(), "markets": rows}))
    tmp.replace(path)
    return len(rows)


def load_watchlist(path: Path) -> tuple[list[WeatherMarket], float]:
    """(markets, age_seconds). Missing/corrupt file → ([], inf) — the intraday
    loop then waits for the next full scan instead of guessing."""
    if not path.exists():
        return [], float("inf")
    try:
        d = json.loads(path.read_text())
        age = time.time() - float(d.get("written_at", 0))
        out = []
        for r in d.get("markets", []):
            r["location"] = Location(**r["location"])
            r["resolution_date"] = datetime.fromisoformat(r["resolution_date"])
            fsd = r.get("forecast_start_date")
            r["forecast_start_date"] = date.fromisoformat(fsd) if fsd else None
            out.append(WeatherMarket(**r))
        return out, age
    except Exception:  # noqa: BLE001 — corrupt state file is never fatal
        return [], float("inf")


def refresh_from_books(scanner, wm: WeatherMarket) -> bool:
    """Fresh EXECUTABLE quotes for an intraday tick: yes_price becomes the mid
    of the live YES book (the hour-old Gamma quote is exactly what the intraday
    loop must not trade on), and both sides' depth/quotes update so Gates 5/5.5
    judge the current book. False → books unusable this tick; skip the market
    rather than price edge off stale data."""
    ys = scanner._fetch_book_summary(wm.yes_token_id)
    ya, yb = ys.get("best_ask", 0.0), ys.get("best_bid", 0.0)
    if not (ya > 0.0 and yb > 0.0):
        return False
    ns = scanner._fetch_book_summary(wm.no_token_id)
    wm.yes_price = round((ya + yb) / 2.0, 4)
    wm.book_depth_usd = ys.get("depth_usd", 0.0)
    wm.yes_best_ask, wm.yes_best_bid = ya, yb
    wm.no_book_depth_usd = ns.get("depth_usd", 0.0)
    wm.no_best_ask = ns.get("best_ask", 0.0)
    wm.no_best_bid = ns.get("best_bid", 0.0)
    return True
