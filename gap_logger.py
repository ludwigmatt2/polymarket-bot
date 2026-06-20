#!/usr/bin/env python3
"""
Gap Logger — Phase 1: Polymarket ↔ Kalshi price gap scanner.

Modes:
  HOSTED (PMXT_API_KEY set): uses Router.fetch_arbitrage() — proper semantic
  matching, best results.

  LOCAL (no key): uses Polymarket CLOB API (via pmxt sidecar) + Kalshi sidecar
  + Jaccard matching — works but finds mostly structural false positives.

Run: python gap_logger.py [--interval 300] [--min-gap 0.005] [--limit 200]
Get a pmxt API key at https://pmxt.dev
"""

import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pmxt
import requests
from dotenv import load_dotenv

load_dotenv()

# ── Configuration ──────────────────────────────────────────────────────────────

SIDECAR_URL = "http://localhost:3847"

MIN_POLY_LIQUIDITY = 500    # USD
MIN_KALSHI_VOLUME = 100     # USD
MIN_JACCARD = 0.18
FEE_ESTIMATE = 0.04

STOPWORDS = {
    "will", "the", "a", "an", "in", "of", "to", "by", "be", "is",
    "or", "and", "for", "on", "at", "no", "yes", "this", "that",
    "2025", "2026", "2027", "2028", "before", "after", "when", "which",
    "who", "what", "how", "many", "much", "next", "any", "his", "her",
}

DATE_PATTERN = re.compile(
    r'\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|'
    r'january|february|march|april|june|july|august|september|october|november|december|'
    r'q[1-4]\s*202\d|by\s+\w+|before\s+\w+|20[2-9]\d)\b',
    re.IGNORECASE,
)
TIMING_PATTERN = re.compile(r'\bwhen\s+will\b|\bwhen\s+does\b', re.IGNORECASE)

LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)

CSV_HEADERS_HOSTED = [
    "timestamp", "buy_venue", "sell_venue",
    "poly_id", "kalshi_id",
    "poly_title", "kalshi_title",
    "spread", "net_spread",
    "buy_price", "sell_price",
    "relation", "confidence",
    "a_exchange", "b_exchange",
    "a_liquidity", "b_liquidity",
    "a_volume", "b_volume",
]

CSV_HEADERS_LOCAL = [
    "timestamp",
    "poly_id", "kalshi_id",
    "poly_title", "kalshi_title",
    "match_score",
    "poly_yes", "kalshi_yes",
    "gap", "net_gap",
    "buy_venue",
    "poly_has_date", "kalshi_is_timing",
    "same_question_type",
    "poly_liquidity", "poly_vol24h",
    "kalshi_volume", "kalshi_oi",
    "poly_url", "kalshi_url",
]


# ── Tokenizer (local mode) ─────────────────────────────────────────────────────

def tokenize(title: str) -> frozenset:
    tokens = re.findall(r"[a-z]+", title.lower())
    return frozenset(t for t in tokens if t not in STOPWORDS and len(t) > 2)


def jaccard(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# ── Hosted mode (PMXT_API_KEY present) ────────────────────────────────────────

def run_hosted_scan(router: pmxt.Router, args: argparse.Namespace, csv_path: Path) -> None:
    """Use Router.fetch_arbitrage() — proper semantic cross-venue matching."""
    print("  [HOSTED] Fetching arbitrage opportunities...", end=" ", flush=True)
    t0 = time.time()
    opps = router.fetch_arbitrage(
        min_spread=args.min_gap,
        limit=args.limit,
        relations=["identity", "subset"],
    )
    print(f"{len(opps)} found ({time.time()-t0:.1f}s)")

    if not opps:
        print("  No opportunities above threshold.")
        return

    rows = []
    now = datetime.now(timezone.utc).isoformat()
    for opp in opps:
        ma, mb = opp.market_a, opp.market_b
        rows.append({
            "timestamp": now,
            "buy_venue": opp.buy_venue,
            "sell_venue": opp.sell_venue,
            "poly_id": ma.market_id,
            "kalshi_id": mb.market_id,
            "poly_title": ma.title,
            "kalshi_title": mb.title,
            "spread": round(opp.spread, 4),
            "net_spread": round(opp.spread - FEE_ESTIMATE, 4),
            "buy_price": round(opp.buy_price, 4),
            "sell_price": round(opp.sell_price, 4),
            "relation": opp.relation or "",
            "confidence": round(opp.confidence or 0, 3),
            "a_exchange": ma.source_exchange or "",
            "b_exchange": mb.source_exchange or "",
            # Kalshi reports 0 for liquidity; use volume as fallback
            "a_liquidity": round(ma.liquidity or 0, 2),
            "b_liquidity": round(mb.liquidity or 0, 2),
            "a_volume": round(ma.volume or 0, 2),
            "b_volume": round(mb.volume or 0, 2),
        })

    _log_rows(rows, csv_path, CSV_HEADERS_HOSTED)
    print(f"  Logged {len(rows)} rows → {csv_path}")
    _print_hosted_summary(rows)


def _print_hosted_summary(rows: list[dict]) -> None:
    profitable = [r for r in rows if r["net_spread"] > 0]
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"\n[{ts}] {len(rows)} gaps  |  Net-positive: {len(profitable)}")
    if rows:
        print(f"\n  {'Spread':>7}  {'Net':>7}  {'Buy@':>7}  {'Relation':>8}  {'Conf':>5}  Title")
        print(f"  {'-------':>7}  {'-------':>7}  {'-------':>7}  {'--------':>8}  {'-----':>5}  ----")
        for r in rows[:10]:
            print(f"  {r['spread']:>7.4f}  {r['net_spread']:>+7.4f}  {r['buy_price']:>7.4f}  "
                  f"{r['relation']:>8}  {r['confidence']:>5.3f}  {r['poly_title'][:50]}")


# ── Local mode (no API key) ────────────────────────────────────────────────────

def _sidecar_token() -> str:
    return pmxt.Kalshi()._server_manager.get_server_info()["accessToken"]


def _fetch_kalshi_all(token: str) -> list[dict]:
    r = requests.post(
        f"{SIDECAR_URL}/api/kalshi/fetchMarkets",
        json={"params": {"limit": 100000, "status": "open"}},
        headers={"x-pmxt-access-token": token, "Content-Type": "application/json"},
        timeout=30,
    )
    r.raise_for_status()
    markets = r.json().get("data", [])
    by_title: dict[str, dict] = {}
    for m in markets:
        title = m.get("title", "")
        vol = float(m.get("volume") or 0)
        if title and (title not in by_title or vol > float(by_title[title].get("volume") or 0)):
            by_title[title] = m
    return [
        m for m in by_title.values()
        if float(m.get("volume") or 0) >= MIN_KALSHI_VOLUME and m.get("yes")
    ]


def _fetch_poly_markets(limit: int) -> list[dict]:
    """Fetch active Polymarket markets via pmxt CLOB sidecar (replaces deprecated Gamma API)."""
    poly = pmxt.Polymarket()
    raw: list[pmxt.UnifiedMarket] = []
    try:
        result = poly.fetch_markets_paginated(params={"limit": min(limit, 100), "active": True, "closed": False})
        raw = list(result.data or [])
    except Exception as e:
        print(f"\n  Polymarket fetch error: {e}", file=sys.stderr)
        return []

    markets = []
    for m in raw:
        if not m.yes or m.yes.price is None:
            continue
        if (m.liquidity or 0) < MIN_POLY_LIQUIDITY:
            continue
        yes_p = m.yes.price
        no_p = m.no.price if m.no else (1.0 - yes_p)
        markets.append({
            "question": m.title,
            "outcomePrices": [str(yes_p), str(no_p)],
            "liquidityNum": m.liquidity or 0,
            "liquidity": m.liquidity or 0,
            "conditionId": m.market_id,
            "id": m.market_id,
            "marketSlug": m.slug or "",
            "slug": m.slug or "",
            "volume24hrClob": m.volume_24h or 0,
            "volume24hr": m.volume_24h or 0,
        })

    # Sort by 24h volume descending (CLOB doesn't guarantee sort order)
    markets.sort(key=lambda x: x["volume24hrClob"], reverse=True)
    return markets[:limit]


def _find_gaps(poly_markets: list[dict], kalshi_markets: list[dict], min_gap: float) -> list[dict]:
    kalshi_index = [(m, tokenize(m["title"])) for m in kalshi_markets]
    now = datetime.now(timezone.utc).isoformat()
    gaps = []

    for pm in poly_markets:
        title = pm.get("question", "")
        pt = tokenize(title)
        if not pt:
            continue
        best_s, best_km = 0.0, None
        for km, kt in kalshi_index:
            s = jaccard(pt, kt)
            if s > best_s:
                best_s, best_km = s, km
        if best_s < MIN_JACCARD or best_km is None:
            continue
        try:
            raw = pm.get("outcomePrices", "[]")
            prices = json.loads(raw) if isinstance(raw, str) else raw
            poly_yes = float(prices[0])
        except (IndexError, TypeError, ValueError):
            continue
        # `yes` may be a {"price": ...} dict or a bare scalar price depending on
        # the sidecar payload — handle both rather than assuming a dict.
        yv = best_km.get("yes")
        kalshi_yes = float((yv.get("price") if isinstance(yv, dict) else yv) or 0) or None
        if not kalshi_yes or poly_yes <= 0 or poly_yes >= 1:
            continue

        gap = round(abs(poly_yes - kalshi_yes), 4)
        if gap < min_gap:
            continue

        poly_has_date = bool(DATE_PATTERN.search(title))
        kalshi_is_timing = bool(TIMING_PATTERN.search(best_km["title"]))
        same_question_type = not kalshi_is_timing and (
            poly_has_date == bool(DATE_PATTERN.search(best_km["title"]))
        )
        slug = pm.get("marketSlug") or pm.get("slug") or ""

        gaps.append({
            "timestamp": now,
            "poly_id": pm.get("conditionId") or pm.get("id"),
            "kalshi_id": best_km.get("marketId") or best_km.get("id"),
            "poly_title": title,
            "kalshi_title": best_km["title"],
            "match_score": round(best_s, 3),
            "poly_yes": poly_yes,
            "kalshi_yes": kalshi_yes,
            "gap": gap,
            "net_gap": round(gap - FEE_ESTIMATE, 4),
            "buy_venue": "polymarket" if poly_yes < kalshi_yes else "kalshi",
            "poly_has_date": poly_has_date,
            "kalshi_is_timing": kalshi_is_timing,
            "same_question_type": same_question_type,
            "poly_liquidity": round(float(pm.get("liquidityNum") or pm.get("liquidity") or 0), 2),
            "poly_vol24h": round(float(pm.get("volume24hrClob") or pm.get("volume24hr") or 0), 2),
            "kalshi_volume": round(float(best_km.get("volume") or 0), 2),
            "kalshi_oi": round(float(best_km.get("openInterest") or best_km.get("open_interest") or 0), 2),
            "poly_url": f"https://polymarket.com/event/{slug}",
            "kalshi_url": f"https://kalshi.com/markets/{best_km.get('marketId','')}",
        })

    return sorted(gaps, key=lambda x: x["gap"], reverse=True)


def run_local_scan(token: str, args: argparse.Namespace, csv_path: Path) -> None:
    """Jaccard keyword matching — fallback when no pmxt API key."""
    t0 = time.time()
    print("  [LOCAL] Fetching Kalshi markets...", end=" ", flush=True)
    kalshi_markets = _fetch_kalshi_all(token)
    print(f"{len(kalshi_markets)} unique/active ({time.time()-t0:.1f}s)")

    t1 = time.time()
    print("  [LOCAL] Fetching Polymarket markets...", end=" ", flush=True)
    poly_markets = _fetch_poly_markets(args.limit)
    print(f"{len(poly_markets)} loaded ({time.time()-t1:.1f}s)")

    t2 = time.time()
    print("  [LOCAL] Matching gaps...", end=" ", flush=True)
    gaps = _find_gaps(poly_markets, kalshi_markets, args.min_gap)
    print(f"{len(gaps)} found ({time.time()-t2:.2f}s)")

    if gaps:
        _log_rows(gaps, csv_path, CSV_HEADERS_LOCAL)
        print(f"  Logged → {csv_path}")

    profitable = [g for g in gaps if g["net_gap"] > 0]
    genuine = [g for g in gaps if g["same_question_type"] and not g["kalshi_is_timing"]]
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"\n[{ts}] {len(poly_markets)} Poly × {len(kalshi_markets)} Kalshi | scan={time.time()-t0:.1f}s")
    print(f"  Gaps: {len(gaps)}  |  Net+: {len(profitable)}  |  Genuine (same type): {len(genuine)}")

    if gaps:
        print(f"\n  {'Gap':>6}  {'Net':>6}  {'Buy@':>7}  {'Score':>5}  {'SameType':>8}  Title")
        print(f"  {'-'*6}  {'-'*6}  {'-'*7}  {'-'*5}  {'-'*8}  {'-'*45}")
        for g in gaps[:10]:
            buy_p = g["poly_yes"] if g["buy_venue"] == "polymarket" else g["kalshi_yes"]
            same = "YES" if g["same_question_type"] else "no"
            print(f"  {g['gap']:>6.3f}  {g['net_gap']:>+6.3f}  {buy_p:>7.4f}  "
                  f"{g['match_score']:>5.2f}  {same:>8}  {g['poly_title'][:45]}")


# ── Shared ────────────────────────────────────────────────────────────────────

def _log_rows(rows: list[dict], csv_path: Path, headers: list[str]) -> None:
    is_new = not csv_path.exists()
    with open(csv_path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
        if is_new:
            writer.writeheader()
        writer.writerows(rows)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket ↔ Kalshi gap logger")
    parser.add_argument("--interval", type=int, default=0,
                        help="Re-scan interval in seconds (0 = run once)")
    parser.add_argument("--limit", type=int, default=200,
                        help="Max markets / arb opportunities to fetch (default: 200)")
    parser.add_argument("--min-gap", type=float, default=0.005,
                        help="Minimum spread to log (default: 0.005)")
    args = parser.parse_args()

    api_key = os.getenv("PMXT_API_KEY")
    mode = "HOSTED" if api_key else "LOCAL (no PMXT_API_KEY)"
    print(f"Polymarket ↔ Kalshi Gap Logger  [{mode}]")
    print(f"  Min gap: {args.min_gap:.1%}  |  Fee est: {FEE_ESTIMATE:.0%}  |  Limit: {args.limit}")
    if not api_key:
        print("  Tip: set PMXT_API_KEY in .env to unlock semantic matching")
    print()

    router = pmxt.Router(pmxt_api_key=api_key) if api_key else None
    token = None if api_key else _sidecar_token()

    today = datetime.now().strftime("%Y%m%d")
    prefix = "hosted" if api_key else "local"
    csv_path = LOG_DIR / f"gaps_{prefix}_{today}.csv"

    if args.interval > 0:
        print(f"  Running every {args.interval}s. Ctrl+C to stop.\n")
        scan_count = 0
        while True:
            scan_count += 1
            print(f"--- Scan #{scan_count} ---")
            try:
                if router:
                    run_hosted_scan(router, args, csv_path)
                else:
                    run_local_scan(token, args, csv_path)
            except Exception as e:
                print(f"  Error: {e}", file=sys.stderr)
            time.sleep(args.interval)
    else:
        if router:
            run_hosted_scan(router, args, csv_path)
        else:
            run_local_scan(token, args, csv_path)


if __name__ == "__main__":
    main()
