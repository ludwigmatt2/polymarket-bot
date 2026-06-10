"""
Polymarket weather market scanner.

Searches Polymarket for active weather-related markets via keyword queries,
parses market titles to extract location/metric/threshold/direction,
and returns structured WeatherMarket objects ready for model evaluation.

Unparseable markets are logged to logs/unparseable_markets.csv for iteration.
"""

from __future__ import annotations

import csv
import json
import logging
import re
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pmxt

_log = logging.getLogger(__name__)

from .config import (
    MAX_DAYS_TO_RESOLUTION,
    MIN_FETCHED_FOR_PARSE_ALARM,
    MIN_MARKET_LIQUIDITY_USD,
    MIN_PARSE_RATE,
    WEATHER_SEARCH_TERMS,
)
from .models import Location, WeatherMarket
from .weather_client import WeatherClient

# Patterns for extracting weather signal from market titles
_DEGREE_PATTERN = re.compile(
    r"(\d+(?:\.\d+)?)\s*°?\s*([CF])\b", re.IGNORECASE
)
# Match city name: everything between "in/at/for" and the next verb or preposition
# Non-greedy so it stops at the earliest "be", "on", "exceed", etc.
_CITY_CANDIDATES = re.compile(
    r"(?:in|at|for|near)\s+([A-Za-z][a-zA-Z\s]{1,25}?)(?=\s+(?:be\b|on\b|by\b|before\b|will\b|exceed\b|above\b|below\b|reach\b|\d|[,?]))",
    re.IGNORECASE,
)
_ABOVE_BELOW = re.compile(
    r"\b(above|exceed|over|high(?:er)? than|greater than|below|under|low(?:er)? than|less than|"
    r"or (?:above|below|more|less|higher|lower)|"
    r"(?:and|or) (?:higher|lower))\b",
    re.IGNORECASE,
)
# "will it be 28°C?" — exact temperature value market
_EXACT_TEMP = re.compile(r"\bwill.*?be\s+(\d+(?:\.\d+)?)\s*°?\s*([CF])\b", re.IGNORECASE)
# "between 72-73°F" — temperature range market (common for F markets)
_TEMP_RANGE = re.compile(
    r"between\s+(\d+(?:\.\d+)?)\s*(?:-|and)\s*(\d+(?:\.\d+)?)\s*°?\s*([CF])\b",
    re.IGNORECASE,
)


def _to_celsius(value: float, unit: str) -> float:
    return value if unit == "C" else (value - 32) * 5 / 9
_TEMPERATURE_WORDS = re.compile(
    r"\b(temperature|temp|high|low|degrees?|fahrenheit|celsius|°[CF])\b",
    re.IGNORECASE,
)
_PRECIPITATION_WORDS = re.compile(
    r"\b(rain(?:fall)?|precipitation|snow(?:fall)?|inches? of rain|mm of rain)\b",
    re.IGNORECASE,
)
# Match precipitation thresholds like "5mm", "2.5 inches", "10 mm of rain"
_PRECIP_THRESHOLD = re.compile(
    r"(\d+(?:\.\d+)?)\s*(mm|inches?|in)\b",
    re.IGNORECASE,
)
# Match range format: "between 5-10mm", "between 5 and 10mm", "between 0.5 and 1 inches"
_PRECIP_RANGE = re.compile(
    r"between\s+(\d+(?:\.\d+)?)\s*(?:-|and)\s*(\d+(?:\.\d+)?)\s*(mm|inches?|in)\b",
    re.IGNORECASE,
)
# Month name in title → for monthly aggregate markets
_MONTH_NAME = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\b",
    re.IGNORECASE,
)

_BELOW_WORDS = {"below", "under", "lower", "less"}

UNPARSEABLE_LOG = Path("logs/unparseable_markets.csv")
MISMATCH_LOG = Path("logs/parser_mismatch.csv")
SCANNER_ALARM_LOG = Path("logs/scanner_alarm.csv")
_ALARM_FIELDS = ["timestamp", "source", "search_terms", "reason"]
_UNPARSEABLE_FIELDS = ["scanned_at", "market_id", "title", "yes_price", "liquidity"]
_MISMATCH_FIELDS = [
    "market_id", "title", "description",
    "parsed_metric", "parsed_threshold", "parsed_threshold_high",
    "parsed_direction", "scanned_at",
]
# Sentinel returned by _parse_market when description disagrees with title.
# Distinct from None (truly unparseable) so scan() doesn't double-log it.
_MISMATCH_DROP = object()


def _append_csv_log(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    """Append rows to a CSV log, writing headers only on first use."""
    is_new = not path.exists()
    path.parent.mkdir(exist_ok=True)
    with open(path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if is_new:
            writer.writeheader()
        writer.writerows(rows)


def _extract_direction(above_below_match) -> str:
    """Return "below" or "above" from a matched _ABOVE_BELOW regex group."""
    if above_below_match:
        word = above_below_match.group(1).lower()
        if any(w in word for w in _BELOW_WORDS):
            return "below"
    return "above"


class _YesSide:
    def __init__(self, price: float):
        self.price = price


class _GammaMarket:
    """Adapter wrapping a Gamma API market dict to match the pmxt.UnifiedMarket interface."""

    def __init__(self, market: dict, event: dict):
        self.market_id: str = market.get("conditionId", "")
        prices_raw = market.get("outcomePrices", "[]")
        prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
        self.yes = _YesSide(float(prices[0])) if prices else None
        self.liquidity: float = float(market.get("volumeClob") or market.get("volume") or 0.0)
        event_title = event.get("title", "")
        question = market.get("question", "")
        self.title: str = f"{event_title} - {question}" if event_title and question else (event_title or question)
        self.description: str = market.get("description") or event.get("description") or ""
        end_raw = market.get("endDate") or market.get("endDateIso") or ""
        try:
            self.resolution_date = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            self.resolution_date = None
        slug = event.get("slug", "")
        self.url: str = f"https://polymarket.com/event/{slug}" if slug else ""


_GAMMA_SEARCH_URL = "https://gamma-api.polymarket.com/public-search"


class WeatherMarketScanner:
    def __init__(
        self,
        source: str = "gamma",
        poly: object | None = None,
        weather_client: WeatherClient | None = None,
    ):
        """
        source: "gamma" (default) uses the Gamma REST API;
                "clob" uses the pmxt CLOB sidecar fetch_events.
        poly / weather_client: optional injectable dependencies for testing.
        """
        self._source = source
        self._poly = poly if poly is not None else self._make_default_poly()
        self._weather_client = weather_client or WeatherClient()
        self._geocache: dict[str, Location | None] = {}

    @staticmethod
    def _make_default_poly():
        # Weather scanning only needs public market data via the local CLOB sidecar,
        # which supports keyword search. The hosted PMXT API (PMXT_API_KEY) rejects
        # the `query`/`status` params, so we create the client in local mode.
        # Strip credentials that aren't needed for public market search and that
        # newer pmxt versions now validate strictly (rejecting placeholder values).
        import os
        _pmxt_key   = os.environ.pop("PMXT_API_KEY", None)
        _poly_pk    = os.environ.pop("POLYMARKET_PRIVATE_KEY", None)
        _poly_proxy = os.environ.pop("POLYMARKET_PROXY_ADDRESS", None)
        try:
            return pmxt.Polymarket()
        finally:
            if _pmxt_key:   os.environ["PMXT_API_KEY"] = _pmxt_key
            if _poly_pk:    os.environ["POLYMARKET_PRIVATE_KEY"] = _poly_pk
            if _poly_proxy: os.environ["POLYMARKET_PROXY_ADDRESS"] = _poly_proxy

    def scan(self) -> list[WeatherMarket]:
        """
        Search Polymarket for active weather markets, parse them, return tradeable ones.
        Also logs unparseable markets for inspection and prints a diagnostic breakdown.
        """
        raw_markets = self._search_keywords()
        print(f"    fetched {len(raw_markets)} unique markets from {len(WEATHER_SEARCH_TERMS)} keyword searches")

        parsed: list[WeatherMarket] = []
        unparseable: list[dict] = []

        for m in raw_markets:
            result = self._parse_market(m)
            if isinstance(result, WeatherMarket):
                parsed.append(result)
            elif result is _MISMATCH_DROP:
                pass  # already logged to parser_mismatch.csv; not an unparseable market
            else:
                unparseable.append({
                    "scanned_at": datetime.utcnow().isoformat(),
                    "market_id": m.market_id,
                    "title": m.title,
                    "yes_price": m.yes.price if m.yes else None,
                    "liquidity": m.liquidity,
                })

        if unparseable:
            _append_csv_log(UNPARSEABLE_LOG, unparseable, _UNPARSEABLE_FIELDS)

        print(f"    parsed {len(parsed)} / {len(raw_markets)}  ({len(unparseable)} unparseable → logs/unparseable_markets.csv)")

        # Parse-rate alarm: a silent parser regression (e.g. the Jun 2026 E4 bug)
        # shows up as a collapsed parse rate long before anyone notices missing trades.
        if len(raw_markets) >= MIN_FETCHED_FOR_PARSE_ALARM:
            parse_rate = len(parsed) / len(raw_markets)
            if parse_rate < MIN_PARSE_RATE:
                _log.warning(
                    "Parse rate collapsed: %d/%d (%.0f%%) — possible parser regression. "
                    "Check logs/parser_mismatch.csv and logs/unparseable_markets.csv.",
                    len(parsed), len(raw_markets), parse_rate * 100,
                )
                _append_csv_log(SCANNER_ALARM_LOG, [{
                    "timestamp": datetime.utcnow().isoformat(),
                    "source": self._source,
                    "search_terms": ",".join(WEATHER_SEARCH_TERMS[:5]),
                    "reason": f"low_parse_rate:{len(parsed)}/{len(raw_markets)}",
                }], _ALARM_FIELDS)

        tradeable = self._filter_tradeable(parsed)
        # Fetch live CLOB order-book depth only for markets that survived all other filters.
        # This avoids 2 sidecar API calls per scanned market on every scan cycle.
        for wm in tradeable:
            wm.book_depth_usd = self._fetch_book_depth_usd(wm.market_id, wm.yes_price)
        print(f"    tradeable after filters: {len(tradeable)} / {len(parsed)}")
        return tradeable

    def _search_keywords(self) -> list[_GammaMarket]:
        """Query Polymarket for each weather keyword, deduplicate by condition_id.

        Uses the configured source (gamma or clob). On zero results logs a
        WARNING and writes a row to logs/scanner_alarm.csv so the Telegram
        bot can surface it.
        """
        if self._source == "clob":
            markets = self._clob_search_keywords()
        else:
            markets = self._gamma_search_keywords()

        if len(markets) == 0:
            _log.warning(
                "Zero markets returned from %s search (terms: %s). "
                "Check logs/scanner_alarm.csv.",
                self._source,
                WEATHER_SEARCH_TERMS[:3],
            )
            _append_csv_log(SCANNER_ALARM_LOG, [{
                "timestamp": datetime.utcnow().isoformat(),
                "source": self._source,
                "search_terms": ",".join(WEATHER_SEARCH_TERMS[:5]),
                "reason": "zero_markets_returned",
            }], _ALARM_FIELDS)

        return markets

    def _gamma_search_keywords(self) -> list[_GammaMarket]:
        """Gamma REST API keyword search."""
        seen_ids: set[str] = set()
        markets: list[_GammaMarket] = []

        for term in WEATHER_SEARCH_TERMS:
            try:
                url = f"{_GAMMA_SEARCH_URL}?q={urllib.parse.quote(term)}&limit=50"
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = json.loads(resp.read())
                for event in data.get("events", []):
                    for m in event.get("markets", []):
                        cid = m.get("conditionId", "")
                        if cid and cid not in seen_ids:
                            seen_ids.add(cid)
                            markets.append(_GammaMarket(m, event))
            except Exception as e:
                print(f"    warning: search failed for {term!r}: {e}", flush=True)

        return markets

    def _clob_search_keywords(self) -> list[_GammaMarket]:
        """CLOB sidecar keyword search via pmxt fetch_events."""
        seen_ids: set[str] = set()
        markets: list[_GammaMarket] = []

        for term in WEATHER_SEARCH_TERMS:
            try:
                events = self._poly.fetch_events({"query": term, "active": True, "limit": 50})
                for event in events:
                    event_dict = {
                        "title": getattr(event, "title", ""),
                        "description": getattr(event, "description", ""),
                        "slug": getattr(event, "slug", ""),
                    }
                    for mkt in getattr(event, "markets", []):
                        cid = getattr(mkt, "condition_id", "") or getattr(mkt, "id", "")
                        if not cid or cid in seen_ids:
                            continue
                        seen_ids.add(cid)
                        yes_price = 0.5
                        if hasattr(mkt, "yes") and mkt.yes is not None:
                            yes_price = float(getattr(mkt.yes, "price", 0.5))
                        mkt_dict = {
                            "conditionId": cid,
                            "outcomePrices": json.dumps([yes_price, 1 - yes_price]),
                            "volumeClob": str(getattr(mkt, "volume", 0)),
                            "question": getattr(mkt, "question", ""),
                            "description": getattr(mkt, "description", event_dict.get("description", "")),
                            "endDate": (getattr(mkt, "end_date_iso", "") or
                                        getattr(mkt, "end_date", "") or ""),
                        }
                        markets.append(_GammaMarket(mkt_dict, event_dict))
            except Exception as e:
                print(f"    warning: CLOB search failed for {term!r}: {e}", flush=True)

        return markets

    def _parse_market(self, m: _GammaMarket) -> WeatherMarket | None:
        """
        Attempt to extract weather signal from a market.
        Returns None if the market title cannot be interpreted as a binary weather bet.
        """
        title = m.title or ""

        # Must mention temperature or precipitation
        is_temp = bool(_TEMPERATURE_WORDS.search(title))
        is_precip = bool(_PRECIPITATION_WORDS.search(title))
        if not is_temp and not is_precip:
            return None

        threshold_high: float | None = None
        forecast_start_date: date | None = None
        above_below = _ABOVE_BELOW.search(title)

        if is_precip and not is_temp:
            metric = "precipitation_sum"
            range_match = _PRECIP_RANGE.search(title)
            if range_match:
                lo_raw = float(range_match.group(1))
                hi_raw = float(range_match.group(2))
                factor = 25.4 if range_match.group(3).lower().startswith("in") else 1.0
                threshold = lo_raw * factor
                threshold_high = hi_raw * factor
                direction = "range"
            else:
                single_match = _PRECIP_THRESHOLD.search(title)
                if not single_match:
                    return None
                factor = 25.4 if single_match.group(2).lower().startswith("in") else 1.0
                threshold = float(single_match.group(1)) * factor
                direction = _extract_direction(above_below)
        else:
            range_match = _TEMP_RANGE.search(title)
            if range_match:
                unit = range_match.group(3).upper()
                threshold = _to_celsius(float(range_match.group(1)), unit)
                threshold_high = _to_celsius(float(range_match.group(2)), unit)
                direction = "range"
            else:
                exact_match = _EXACT_TEMP.search(title)
                degree_match = exact_match or _DEGREE_PATTERN.search(title)
                if not degree_match:
                    return None

                unit = degree_match.group(2).upper()
                threshold = _to_celsius(float(degree_match.group(1)), unit)

                if exact_match and not above_below:
                    direction = "equal"
                else:
                    direction = _extract_direction(above_below)

            title_lower = title.lower()
            metric = (
                "temperature_2m_min"
                if "lowest" in title_lower or "low temperature" in title_lower
                else "temperature_2m_max"
            )

        # City extraction — try pattern match first, fall back to first capitalized sequence
        location = self._extract_location(title)
        if location is None:
            return None

        # Resolution date from market
        resolution_date = m.resolution_date
        if resolution_date is None:
            return None

        # Resolution source (heuristic from description)
        description = m.description or ""
        source = "NOAA" if "NOAA" in description else (
            "Weather Underground" if "Weather Underground" in description.lower() else "unknown"
        )

        if m.yes is None or m.yes.price is None:
            return None

        # For monthly aggregate markets, set forecast_start_date to the 1st of the named month
        res_date = resolution_date if resolution_date.tzinfo else resolution_date.replace(tzinfo=timezone.utc)
        days_out = (res_date - datetime.now(timezone.utc)).days
        if days_out > 7 and metric == "precipitation_sum":
            month_m = _MONTH_NAME.search(title)
            if month_m:
                mo = datetime.strptime(month_m.group(1), "%B").month
                forecast_start_date = date(res_date.year, mo, 1)

        # E4: cross-check parsed values against description to catch mis-parses
        if not self._description_agrees(m.description, metric, threshold, threshold_high, direction):
            _append_csv_log(MISMATCH_LOG, [{
                "market_id": m.market_id,
                "title": title,
                "description": (m.description or "")[:200],
                "parsed_metric": metric,
                "parsed_threshold": threshold,
                "parsed_threshold_high": threshold_high or "",
                "parsed_direction": direction,
                "scanned_at": datetime.utcnow().isoformat(),
            }], _MISMATCH_FIELDS)
            return _MISMATCH_DROP

        return WeatherMarket(
            market_id=m.market_id,
            title=title,
            yes_price=m.yes.price,
            liquidity_usd=m.liquidity or 0.0,
            resolution_date=resolution_date,
            resolution_source=source,
            location=location,
            metric=metric,
            threshold=threshold,
            threshold_high=threshold_high,
            direction=direction,
            url=m.url or "",
            raw_title=title,
            forecast_start_date=forecast_start_date,
        )

    def _description_agrees(
        self,
        description: str,
        metric: str,
        threshold: float,
        threshold_high: float | None,
        direction: str,
    ) -> bool:
        """
        E4: Lightly parse the description and verify it agrees with title-derived values.
        Returns True if description is absent/unparseable (no evidence of conflict) or agrees.
        Returns False only on a clear conflict between description and title.
        """
        if not description:
            return True

        # Strip example/parenthetical values before extracting thresholds — Polymarket
        # boilerplate includes "(eg, 9.1°C)" in every daily-temp description, which is
        # not a threshold and must not count as conflicting evidence.
        description = re.sub(r"\((?:e\.?g\.?|example)[^)]*\)", "", description, flags=re.IGNORECASE)

        desc = description.lower()

        # Check direction agreement when description has explicit above/below language
        above_kw = any(w in desc for w in ("above", "exceed", "greater than", "more than", "higher than", "at least"))
        below_kw = any(w in desc for w in ("below", "less than", "under", "lower than", "fewer than"))
        if above_kw and not below_kw and direction == "below":
            return False
        if below_kw and not above_kw and direction == "above":
            return False

        # Check threshold agreement for temperature (allow ±3°C tolerance for F→C conversion rounding)
        if metric.startswith("temperature"):
            deg_matches = _DEGREE_PATTERN.findall(description)
            if deg_matches:
                desc_values_c = [_to_celsius(float(v), u) for v, u in deg_matches]
                # Check if any description value is close to the title threshold
                close_to_threshold = any(abs(v - threshold) <= 3.0 for v in desc_values_c)
                # If ALL description values are far from the threshold, it's a conflict
                if not close_to_threshold:
                    # Also check against threshold_high for range markets
                    if threshold_high is not None:
                        close_to_high = any(abs(v - threshold_high) <= 3.0 for v in desc_values_c)
                        if not close_to_high:
                            return False
                    else:
                        return False

        # Check threshold agreement for precipitation (allow ±5mm tolerance)
        if metric == "precipitation_sum":
            precip_matches = _PRECIP_THRESHOLD.findall(description)
            if precip_matches:
                desc_values_mm = [
                    float(v) * (25.4 if u.lower().startswith("in") else 1.0)
                    for v, u in precip_matches
                ]
                close_to_threshold = any(abs(v - threshold) <= 5.0 for v in desc_values_mm)
                if not close_to_threshold:
                    if threshold_high is not None:
                        close_to_high = any(abs(v - threshold_high) <= 5.0 for v in desc_values_mm)
                        if not close_to_high:
                            return False
                    else:
                        return False

        return True

    def _fetch_book_depth_usd(self, market_id: str, yes_price: float) -> float:
        """
        B4: Fetch live CLOB order-book depth for the YES side.
        Returns 0.0 when the pmxt sidecar is unavailable (paper mode).
        """
        try:
            market = self._poly.fetch_market(id=market_id)
            yes_outcome = market.yes
            if yes_outcome is None:
                # fall back to first outcome labelled Yes/YES
                for o in market.outcomes:
                    if o.label.upper() == "YES":
                        yes_outcome = o
                        break
            if yes_outcome is None:
                return 0.0
            ob = self._poly.fetch_order_book(yes_outcome.outcome_id)
            # Depth = USD value of asks on the YES side (what we can buy)
            return sum(ask.price * ask.size for ask in ob.asks) if ob.asks else 0.0
        except Exception:
            return 0.0

    def _extract_location(self, title: str) -> Location | None:
        """Try to find a city name in the title and geocode it."""
        matches = _CITY_CANDIDATES.findall(title)
        if matches:
            city = matches[-1].strip()
            loc = self._geocode(city)
            if loc:
                return loc

        before_degree = title.split("°")[0] if "°" in title else title
        words = before_degree.split()
        for i, w in enumerate(words):
            if w[0].isupper() and len(w) > 2 and w.lower() not in {"will", "the", "high", "low"}:
                candidate = " ".join(words[i:i+2]) if i + 1 < len(words) and words[i+1][0].isupper() else w
                loc = self._geocode(candidate)
                if loc:
                    return loc

        return None

    def _geocode(self, city: str) -> Location | None:
        if city not in self._geocache:
            self._geocache[city] = self._weather_client.geocode(city)
        return self._geocache[city]

    def _filter_tradeable(self, markets: list[WeatherMarket]) -> list[WeatherMarket]:
        """Apply liquidity and resolution date filters."""
        now = datetime.now(timezone.utc)
        result = []
        reasons: dict[str, int] = {}
        for m in markets:
            if m.liquidity_usd < MIN_MARKET_LIQUIDITY_USD:
                reasons["low_liquidity"] = reasons.get("low_liquidity", 0) + 1
                continue
            res_date = m.resolution_date if m.resolution_date.tzinfo else m.resolution_date.replace(tzinfo=timezone.utc)
            delta: timedelta = res_date - now
            hours_out = delta.total_seconds() / 3600
            days_out = delta.days
            if hours_out < 0:
                reasons["already_resolved"] = reasons.get("already_resolved", 0) + 1
                continue
            if days_out > MAX_DAYS_TO_RESOLUTION:
                reasons["too_far_out"] = reasons.get("too_far_out", 0) + 1
                continue
            result.append(m)
        if reasons:
            print(f"    filter breakdown: {dict(sorted(reasons.items(), key=lambda x: -x[1]))}")
        return result

