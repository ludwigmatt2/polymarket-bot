"""Station-expansion tooling.

  report        per-station skill from data/logs/expansion_calibration.csv
                (labels pending rows first). expansion=0 rows are the control.
  truth-check   replay CLOSED Polymarket weather events: does station_truth
                (IEM METAR peak, rounded, bucket rule) reproduce the winning
                bucket Polymarket actually settled on? Run per new station
                before trusting its labels.

VPS: sudo -u bot env RAILWAY_VOLUME_MOUNT_PATH=/opt/polymarket-bot/data \
       venv/bin/python scripts/expansion_report.py report|truth-check [--pages N]
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from weather.config import EXPANSION_STATIONS  # noqa: E402
from weather.expansion_log import LOG_NAME, resolve_rows, station_report  # noqa: E402
from weather.paper_trader import _local_event_date  # noqa: E402
from weather.paths import DATA_DIR  # noqa: E402
from weather.station_truth import station_outcome  # noqa: E402

GAMMA = "https://gamma-api.polymarket.com/events"


def cmd_report() -> None:
    path = DATA_DIR / "logs" / LOG_NAME
    print(f"labeled {resolve_rows(path)} pending rows")
    rows = station_report(path)
    if not rows:
        print("no resolved rows yet")
        return
    print(f"{'station':8}{'exp':>4}{'n':>6}{'brier_mdl':>11}{'brier_mkt':>11}{'skill':>8}{'bias':>8}{'onchain':>9}")
    for r in rows:
        skill = 1 - r["brier_model"] / r["brier_market"] if r["brier_market"] else float("nan")
        print(f"{r['station']:8}{r['expansion']:>4}{r['n']:>6}{r['brier_model']:>11.4f}"
              f"{r['brier_market']:>11.4f}{skill:>8.2f}{r['bias']:>8.3f}{r['onchain_frac']:>9.2f}")
    print("skill = 1 - Brier(model)/Brier(market); >0 beats the market. bias = mean(model_p - outcome).")


def cmd_truth_check(pages: int) -> None:
    from weather.market_scanner import WeatherMarketScanner, _GammaMarket

    sc = WeatherMarketScanner()
    stats: dict[str, list[int]] = collections.defaultdict(lambda: [0, 0, 0])  # ok, bad, nodata
    bad: list[str] = []
    cache: dict = {}
    for page in range(pages):
        url = f"{GAMMA}?tag_slug=weather&closed=true&limit=100&offset={page * 100}&order=endDate&ascending=false"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        events = json.loads(urllib.request.urlopen(req, timeout=30).read())
        if not events:
            break
        for ev in events:
            for m in ev.get("markets", []):
                try:
                    prices = json.loads(m.get("outcomePrices") or "[]")
                    if len(prices) != 2 or sorted(float(p) for p in prices) != [0.0, 1.0]:
                        continue
                    won = float(prices[0]) == 1.0
                    wm = sc._parse_market(_GammaMarket(m, ev))
                    if wm is None or not hasattr(wm, "station_icao") or not wm.station_icao:
                        continue
                    day = _local_event_date(wm.resolution_date, wm.location.timezone)
                    out, _src, _v = station_outcome(
                        wm.station_icao, wm.station_country, wm.resolve_unit, day, wm.metric,
                        wm.threshold, wm.threshold_high, wm.direction, cache=cache)
                except Exception:  # noqa: BLE001
                    continue
                s = stats[wm.station_icao]
                if out is None:
                    s[2] += 1
                elif out == won:
                    s[0] += 1
                else:
                    s[1] += 1
                    bad.append(f"{wm.station_icao} {wm.title[:70]} polymarket_yes={won} ours={out}")
    print(f"{'station':8}{'exp':>4}{'agree':>7}{'differ':>8}{'nodata':>8}")
    for icao, (ok, ko, nd) in sorted(stats.items(), key=lambda kv: (icao_exp(kv[0]), kv[0])):
        print(f"{icao:8}{icao_exp(icao):>4}{ok:>7}{ko:>8}{nd:>8}")
    for line in bad[:25]:
        print("DIFF", line)


def icao_exp(icao: str) -> int:
    return int(icao in EXPANSION_STATIONS)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["report", "truth-check"])
    ap.add_argument("--pages", type=int, default=6)
    a = ap.parse_args()
    cmd_report() if a.cmd == "report" else cmd_truth_check(a.pages)
