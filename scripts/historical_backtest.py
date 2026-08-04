"""From-scratch historical backtest: replay real Polymarket markets against a
REPLAYED forecast (ECMWF archive, ~1-day lead), the real historical market
price (CLOB prices-history), and real station truth — the thing paper trading
can only accumulate a few samples of per day, done at scale over real history.

Unlike scripts/phase3_backtest.py / relabel_paper_truth.py (which only re-score
trades the bot already placed, using their stored entry_price/model_p), this
generates NEW hypothetical trades on markets the bot never scanned live.

Reuses production code wherever possible so results are apples-to-apples with
the live bot: WeatherMarketScanner._parse_market for threshold/station parsing,
ProbabilityModel.compute_probability for the signal, station_truth for the
outcome, paper_trader's exact PnL/Brier formulas. What it does NOT replicate:
the full live gate stack (freshness/liquidity/spread/depth gates) or the
lead-time/spread shrinkage in signal_generator — this is raw model edge vs
market price, filtered only by the same MIN_NET_EV_PP/EDGE_SAFETY_MARGIN_PP
threshold. Treat results as an upper bound on a fully-gated live strategy.

KNOWN LIMITATION — single-model spread: the live bot blends 3 model families
(gfs_seamless + icon_seamless + ecmwf_ifs025); this backtest can only replay
ECMWF (the one free walk-forward-replayable archive found so far — see
weather/ecmwf_archive_client.py). A single model's internal perturbation
ensemble is a well-known UNDERDISPERSED proxy for true forecast uncertainty —
it's missing the structural/model disagreement a multi-model blend captures.
Early results show the model's probabilities landing closer to 0/1 than the
market's, and losing on Brier score to the market as a result — this may be
an artifact of the missing GFS/ICON spread rather than a real property of the
live 3-model system. NOAA's GEFS archive (noaa-gefs-pds on S3) was confirmed
to have the same walk-forward-replayable structure as ECMWF's — the next step
to de-bias this backtest is a matching gefs_archive_client.py and blending it
in, before trusting these numbers as a verdict on the live model's edge.

HARD CONSTRAINT — CLOB price history is NOT full market history: despite
`interval=max`, clob.polymarket.com/prices-history only retains a rolling
~30 days regardless of the market's actual age (bisected live: 2026-06-08 ->
0 points, 2026-06-10 -> 11 points, checked on 2026-07-10). This caps a
from-scratch backtest at ~30 days no matter how deep the forecast archive
(ECMWF/GEFS) or Polymarket's own market-listing history goes — there is no
way to get a genuine "full history" backtest via this endpoint. WINDOW_DAYS
reflects this; don't raise it expecting more data to appear.

Run:  venv/bin/python scripts/historical_backtest.py
"""

from __future__ import annotations

import csv
import json
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from weather import ecmwf_archive_client as ecmwf  # noqa: E402
from weather import gefs_archive_client as gefs  # noqa: E402
from weather import station_obs, station_truth  # noqa: E402
from weather.config import EDGE_SAFETY_MARGIN_PP, MIN_NET_EV_PP  # noqa: E402
from weather.market_scanner import WeatherMarketScanner, _GammaMarket  # noqa: E402
from weather.models import EnsembleForecast, WeatherMarket  # noqa: E402
from weather.paper_trader import _brier, _trade_pnl  # noqa: E402
from weather.probability_model import ProbabilityModel  # noqa: E402

_UA = {"User-Agent": "Mozilla/5.0"}
_GAMMA = "https://gamma-api.polymarket.com"
_CLOB_HIST = "https://clob.polymarket.com/prices-history"

# Full scale-out: every registered, live-truth-verified station (see
# weather/iem_client.py._STATION_REGISTRY) except VHHH — Hong Kong has no
# live truth dispatch (see hko_client.py docstring: HKO's CIS data lags ~1
# month, so there's no truth to score these against yet).
SERIES = {
    "10727": "Dallas", "10005": "NYC", "10728": "Miami", "10739": "Atlanta",
    "10742": "Seoul", "11168": "Paris", "11295": "Tel Aviv", "10006": "London",
    "10740": "Tokyo", "11345": "Madrid", "10115": "Dubai", "11314": "Singapore",
    "10741": "Shanghai", "10743": "Toronto", "11366": "Shenzhen",
}
# HARD CONSTRAINT, confirmed empirically: CLOB's /prices-history endpoint
# ("interval=max") does NOT mean unlimited history — it's a rolling ~30-day
# window regardless of how old the market is. Bisected the boundary directly:
# 2026-06-08 event -> 0 price points, 2026-06-10 event -> 11 points (checked
# 2026-07-10). Every city/day older than ~30 days is UNPRICEABLE no matter how
# deep the forecast archive or market-listing history goes — there is no
# "full window" beyond this for a from-scratch backtest via this endpoint.
# 32 gives a couple of days' buffer past the observed boundary.
WINDOW_DAYS = 32
# Blended (ECMWF+GEFS) run writes to a SEPARATE file from the single-model
# pilot (historical_backtest.csv) so the underdispersion comparison survives.
OUT_CSV = Path(__file__).resolve().parent.parent / "data" / "logs" / "historical_backtest_blended.csv"


def _get_json(url: str, timeout: int = 20):
    with urllib.request.urlopen(urllib.request.Request(url, headers=_UA), timeout=timeout) as r:
        return json.loads(r.read().decode())


def fetch_series_events(series_id: str, since: date) -> list[dict]:
    events, offset = [], 0
    while True:
        url = f"{_GAMMA}/events?series_id={series_id}&closed=true&limit=100&offset={offset}"
        batch = _get_json(url)
        if not batch:
            break
        events.extend(batch)
        offset += len(batch)
        if len(batch) < 100:
            break
    out = []
    for e in events:
        end = e.get("endDate") or ""
        try:
            d = datetime.fromisoformat(end.replace("Z", "+00:00")).date()
        except ValueError:
            continue
        if d >= since:
            out.append(e)
    return out


def clob_price_near(token_id: str, target_ts: int) -> float | None:
    url = f"{_CLOB_HIST}?market={token_id}&interval=max&fidelity=60"
    try:
        d = _get_json(url, timeout=30)
    except Exception:  # noqa: BLE001
        return None
    hist = d.get("history") or []
    if not hist:
        return None
    best = min(hist, key=lambda p: abs(p["t"] - target_ts))
    return float(best["p"])


_FIELDS = ["city", "event_day", "title", "direction", "bucket_type", "model_p", "market_p",
           "edge_pp", "entry_price", "actual_outcome", "pnl_usd", "brier", "market_brier",
           "truth_source", "n_members"]


def _already_done(path: Path) -> set[tuple[str, str]]:
    """(city, event_day) pairs already fully processed in a prior (possibly
    killed) run — resuming skips these instead of re-fetching from scratch."""
    if not path.exists():
        return set()
    with open(path, newline="") as f:
        return {(r["city"], r["event_day"]) for r in csv.DictReader(f)}


def summarize(path: Path) -> None:
    if not path.exists():
        print("no results yet"); return
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    n = len(rows)
    if n == 0:
        print("0 trades"); return
    pnl = [float(r["pnl_usd"]) for r in rows]
    wins = sum(1 for p in pnl if p > 0)
    gross_win = sum(p for p in pnl if p > 0)
    gross_loss = -sum(p for p in pnl if p <= 0)
    pf = gross_win / gross_loss if gross_loss else float("inf")
    model_brier = sum(float(r["brier"]) for r in rows) / n
    mkt_brier = sum(float(r["market_brier"]) for r in rows) / n
    print(f"trades: {n}  wins: {wins}  WR: {wins/n:.1%}  "
          f"PnL: ${sum(pnl):.2f}  PF: {pf:.3f}")
    print(f"model Brier: {model_brier:.4f}  market Brier: {mkt_brier:.4f}  "
          f"(model {'BEATS' if model_brier < mkt_brier else 'LOSES TO'} market)")
    by_city: dict[str, list[float]] = {}
    for r in rows:
        by_city.setdefault(r["city"], []).append(float(r["pnl_usd"]))
    for city, p in by_city.items():
        print(f"  {city}: {len(p)} trades, ${sum(p):.2f}")


def main() -> None:
    since = date.today() - timedelta(days=WINDOW_DAYS)
    scanner = WeatherMarketScanner()
    model = ProbabilityModel()
    done = _already_done(OUT_CSV)
    new_file = not OUT_CSV.exists()
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    out_f = open(OUT_CSV, "a", newline="")
    writer = csv.DictWriter(out_f, fieldnames=_FIELDS)
    if new_file:
        writer.writeheader(); out_f.flush()

    # Pass 1: pull every city's closed-event history and group by LOCAL event
    # day. Day-major processing (not city-major) is what lets the bytes cache
    # in ecmwf/gefs_archive_client actually pay off — cities whose event day
    # falls on the same calendar date share the exact same run+step fetches.
    by_day: dict[date, list[tuple[str, object, object]]] = {}
    for series_id, city in SERIES.items():
        events = fetch_series_events(series_id, since)
        print(f"{city}: {len(events)} closed events since {since}", flush=True)
        for event in events:
            for m in event.get("markets", []):
                gm = _GammaMarket(m, event)
                wm = scanner._parse_market(gm)  # noqa: SLF001 — deliberate reuse
                if not (isinstance(wm, WeatherMarket) and wm.station_icao):
                    continue
                event_day = station_obs.local_event_date(wm.resolution_date, wm.location.timezone)
                if (city, event_day.isoformat()) in done:
                    continue
                by_day.setdefault(event_day, []).append((city, wm, gm))

    total_days = len(by_day)
    print(f"\n{total_days} distinct event-days to process across {len(SERIES)} cities\n",
          flush=True)

    n_events = n_markets = n_priced = n_gated = 0

    for day_idx, event_day in enumerate(sorted(by_day)):
        by_city: dict[str, list[tuple[object, object]]] = {}
        for city, wm, gm in by_day[event_day]:
            by_city.setdefault(city, []).append((wm, gm))

        run_date = event_day - timedelta(days=1)
        for city, pairs in by_city.items():
            n_events += 1
            wm0 = pairs[0][0]
            kind = "max" if wm0.metric.endswith("max") else "min"

            # Fail fast: CLOB /prices-history is a rolling ~30-day window
            # regardless of market age (see WINDOW_DAYS comment). Skip the
            # ~100s forecast fetch entirely if this day is already unpriceable.
            probe_ts = int(datetime(run_date.year, run_date.month, run_date.day,
                                     12, tzinfo=timezone.utc).timestamp())
            if clob_price_near(pairs[0][1].yes_token_id, probe_ts) is None:
                print(f"  [{day_idx+1}/{total_days}] {city} {event_day}: "
                      f"no CLOB price history (>30d old), skipping", flush=True)
                continue

            t0 = time.monotonic()
            try:
                ecmwf_members = ecmwf.daily_extreme_members(
                    wm0.location.lat, wm0.location.lon, event_day,
                    wm0.location.timezone, run_date, 0, kind=kind)
            except Exception as exc:  # noqa: BLE001
                print(f"  ! ecmwf fetch failed {city} {event_day}: {exc}", flush=True)
                ecmwf_members = []
            try:
                gefs_members = gefs.daily_extreme_members(
                    wm0.location.lat, wm0.location.lon, event_day,
                    wm0.location.timezone, run_date, 0, kind=kind)
            except Exception as exc:  # noqa: BLE001
                print(f"  ! gefs fetch failed {city} {event_day}: {exc}", flush=True)
                gefs_members = []
            if len(ecmwf_members) < 5 and len(gefs_members) < 5:
                print(f"  ! {city} {event_day}: no usable members from either model, skipping",
                      flush=True)
                continue
            fetch_s = time.monotonic() - t0
            n_members = len(ecmwf_members) + len(gefs_members)

            member_arrays = {}
            if len(ecmwf_members) >= 5:
                member_arrays["ecmwf_ifs025"] = ecmwf_members
            if len(gefs_members) >= 5:
                member_arrays["gfs_seamless"] = gefs_members
            forecast = EnsembleForecast(
                lat=wm0.location.lat, lon=wm0.location.lon, target_date=event_day,
                metric=wm0.metric, member_arrays=member_arrays,
                fetched_at=datetime(run_date.year, run_date.month, run_date.day,
                                     tzinfo=timezone.utc))

            signal_ts = int(datetime(run_date.year, run_date.month, run_date.day,
                                      12, tzinfo=timezone.utc).timestamp())

            n_trades_today = 0
            for wm, gm in pairs:
                n_markets += 1
                prob = model.compute_probability(
                    forecast=forecast, threshold=wm.threshold, direction=wm.direction,
                    threshold_high=wm.threshold_high, lead_day=1, month=event_day.month,
                    resolve_unit=wm.resolve_unit, observed_extreme=None,
                )
                market_p = clob_price_near(gm.yes_token_id, signal_ts)
                if market_p is None or not (0.01 < market_p < 0.99):
                    continue
                n_priced += 1

                model_p = prob.calibrated_p
                edge_pp = abs(model_p - market_p)
                net_ev = edge_pp - EDGE_SAFETY_MARGIN_PP
                if net_ev < MIN_NET_EV_PP:
                    continue
                n_gated += 1
                direction = "YES" if model_p > market_p else "NO"
                entry_price = market_p if direction == "YES" else (1.0 - market_p)

                yes_cond, src, station_val = station_truth.station_outcome(
                    wm.station_icao, wm.station_country, wm.resolve_unit, event_day,
                    wm.metric, wm.threshold, wm.threshold_high, wm.direction)
                if yes_cond is None:
                    continue

                bet_wins = yes_cond if direction == "YES" else not yes_cond
                pnl = _trade_pnl(25.0, entry_price, bet_wins)
                brier = _brier(model_p, yes_cond)
                market_brier = _brier(market_p, yes_cond)

                writer.writerow({
                    "city": city, "event_day": event_day.isoformat(),
                    "title": wm.title[:70], "direction": direction,
                    "bucket_type": wm.direction,
                    "model_p": round(model_p, 4), "market_p": round(market_p, 4),
                    "edge_pp": round(edge_pp, 4), "entry_price": round(entry_price, 4),
                    "actual_outcome": int(yes_cond), "pnl_usd": round(pnl, 4),
                    "brier": round(brier, 4), "market_brier": round(market_brier, 4),
                    "truth_source": src, "n_members": n_members,
                })
                out_f.flush()
                n_trades_today += 1
            print(f"  [{day_idx+1}/{total_days}] {city} {event_day}: {n_members} members "
                  f"(ecmwf={len(ecmwf_members)},gfs={len(gefs_members)}) in {fetch_s:.0f}s, "
                  f"{len(pairs)} buckets -> {n_trades_today} trades", flush=True)

        # Cache is only worth keeping WITHIN a calendar day (cities sharing
        # that day's run+step) — clear before moving on to bound memory.
        ecmwf.clear_cache()
        gefs.clear_cache()

    out_f.close()
    print(f"\nnew events processed this run: {n_events}, markets: {n_markets}, "
          f"priced: {n_priced}, passed edge gate: {n_gated}", flush=True)
    print("\n=== CUMULATIVE SUMMARY (all runs) ===", flush=True)
    summarize(OUT_CSV)


if __name__ == "__main__":
    main()
