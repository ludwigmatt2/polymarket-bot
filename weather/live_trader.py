"""
Live trader — Kelly-sized order execution via the official Polymarket py-clob-client-v2 SDK.

Activated only when paper_trader.compute_stats().ready_for_live == True.
Credentials are passed via the LiveTrader constructor (from the per-user
encrypted store — see weather.secrets).
"""

from __future__ import annotations

import csv
import json
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from ._io import atomic_write_csv, atomic_write_json
from .config import (
    DAILY_LOSS_LIMIT_PCT,
    KELLY_FRACTION,
    MAX_LIVE_TRADE_USD,
)
from .models import Location, Signal
from .paper_trader import PaperTrader, _brier, _evaluate_outcome
from .secrets import _LEGACY_SIG_MAP

from .paths import DATA_DIR as _DATA_DIR
LIVE_TRADES_LOG = _DATA_DIR / "logs" / "live_trades.csv"
_IDEMPOTENCY_FILE = _DATA_DIR / "logs" / "live_idempotency.json"
_RUNTIME_CONFIG = _DATA_DIR / "logs" / "runtime_config.json"

_CSV_HEADERS = [
    "trade_id", "market_id", "market_title", "order_id",
    "signal_time", "direction", "entry_price", "model_p",
    "size_usd", "kelly_fraction", "edge_pp",
    # Fill results (set after fill confirmation)
    "filled_size", "filled_price", "order_status",
    # Submission metadata
    "submitted_at", "error",
    # Resolution metadata — stored at submit time so auto-resolve needs no re-parsing
    "resolution_date", "metric", "threshold", "threshold_high",
    "weather_direction", "lat", "lon", "location_tz",
    # Resolution results
    "actual_outcome", "resolved_at", "pnl_usd", "brier_score",
    # Tax fields — EUR equivalent at resolve date (ECB reference rate)
    "eur_rate", "pnl_eur",
]


def _fetch_ecb_rate(dt: "date") -> "float | None":
    """ECB reference rate: USD → EUR for given date. Tries up to 4 days back for weekends/holidays."""
    import urllib.request
    from datetime import timedelta
    for offset in range(4):
        d = (dt - timedelta(days=offset)).isoformat()
        url = (
            "https://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A"
            f"?format=csvdata&startPeriod={d}&endPeriod={d}"
        )
        try:
            with urllib.request.urlopen(url, timeout=8) as resp:
                lines = resp.read().decode().splitlines()
            if len(lines) < 2:
                continue
            headers = [h.strip() for h in lines[0].split(",")]
            if "OBS_VALUE" not in headers:
                continue
            idx = headers.index("OBS_VALUE")
            for line in lines[1:]:
                parts = line.split(",")
                if len(parts) > idx and parts[idx].strip():
                    try:
                        return float(parts[idx])
                    except ValueError:
                        continue
        except Exception:
            continue
    return None


def _get_max_trade_usd() -> float:
    """Live cap per trade — reads runtime override first, falls back to config."""
    try:
        if _RUNTIME_CONFIG.exists():
            cfg = json.loads(_RUNTIME_CONFIG.read_text())
            if "max_trade_usd" in cfg:
                return float(cfg["max_trade_usd"])
    except Exception:
        pass
    return MAX_LIVE_TRADE_USD


def _make_clob_client(
    pk: str,
    funder_address: str | None = None,
    signature_type: int | str | None = None,
    clob_api_key: str | None = None,
    clob_secret: str | None = None,
    clob_passphrase: str | None = None,
) -> Any:
    """Build an authenticated ClobClient. Normalises signature_type to int."""
    from py_clob_client_v2 import ClobClient
    if isinstance(signature_type, str):
        sig_type: int = _LEGACY_SIG_MAP.get(signature_type.lower(), 0)
    elif signature_type is None:
        sig_type = 0 if not funder_address else 1
    else:
        sig_type = signature_type
    creds = None
    if clob_api_key:
        from py_clob_client_v2.clob_types import ApiCreds
        creds = ApiCreds(
            api_key=clob_api_key,
            api_secret=clob_secret or "",
            api_passphrase=clob_passphrase or "",
        )
    return ClobClient(
        host="https://clob.polymarket.com",
        chain_id=137,
        key=pk,
        creds=creds,
        signature_type=sig_type,
        funder=funder_address,
    )


_USDC_BASE_UNITS = 1_000_000  # CLOB returns USDC collateral in 6-decimal base units


def _read_collateral_balance(client) -> float:
    """Available USDC collateral in dollars.

    The CLOB /balance-allowance endpoint returns the amount as a string in
    6-decimal base units (e.g. "7330000" == $7.33), so scale by 1e6.
    """
    from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
    result = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
    raw = result.get("balance", "0") or "0"
    return float(raw) / _USDC_BASE_UNITS


_geoblock_cache: "tuple[float, dict] | None" = None


def check_geoblock(ttl: float = 300.0) -> dict | None:
    """Whether the current egress IP may place orders on Polymarket.

    Hits the geoblock endpoint (on polymarket.com, not the CLOB API) and returns
    {'blocked': bool, 'country': str, 'region': str, 'ip': str}, or None if the
    check itself fails — None means "unknown, proceed" (the order POST is the
    real gate). Successful results are cached for `ttl`s since the egress IP is
    stable within a run. Honours HTTPS_PROXY like every other call, so it reports
    the same region the /order endpoint will see.
    """
    global _geoblock_cache
    now = time.monotonic()
    if _geoblock_cache is not None and now - _geoblock_cache[0] < ttl:
        return _geoblock_cache[1]
    import requests
    try:
        r = requests.get("https://polymarket.com/api/geoblock", timeout=8)
        r.raise_for_status()
        result = r.json()
    except Exception:
        return None
    _geoblock_cache = (now, result)
    return result


def fetch_balance_for_creds(creds: dict) -> float:
    """Read available USDC for a user's stored creds via the official CLOB SDK.

    Shared by the onboarding wizard so balance display goes through the same
    path as live trading (no pmxt dependency). `creds` is the dict returned by
    weather.secrets.get_user_creds (pk + funder_address + integer signature_type
    + optional clob_* L2 creds).
    """
    client = _make_clob_client(
        pk=creds.get("pk", ""),
        funder_address=creds.get("funder_address"),
        signature_type=creds.get("signature_type"),
        clob_api_key=creds.get("clob_api_key"),
        clob_secret=creds.get("clob_secret"),
        clob_passphrase=creds.get("clob_passphrase"),
    )
    return _read_collateral_balance(client)


class LiveTrader:
    def __init__(
        self,
        paper_trader: PaperTrader,
        bankroll_usd: float,
        fill_poll_delay: float = 2.0,
        log_path: Path = LIVE_TRADES_LOG,
        idempotency_path: Path = _IDEMPOTENCY_FILE,
        private_key: str | None = None,
        funder_address: str | None = None,
        signature_type: int | str | None = None,
        clob_api_key: str | None = None,
        clob_secret: str | None = None,
        clob_passphrase: str | None = None,
        # Backward-compat alias — callers using the old field name still work
        proxy_address: str | None = None,
    ):
        self.paper_trader = paper_trader
        self.bankroll_usd = bankroll_usd
        self._fill_poll_delay = fill_poll_delay  # override to 0 in tests
        self._log_path = log_path
        self._idempotency_path = idempotency_path
        self._private_key = private_key
        self._funder_address = funder_address or proxy_address  # accept legacy name
        self._signature_type = signature_type  # normalised to int by _make_clob_client
        self._clob_api_key = clob_api_key
        self._clob_secret = clob_secret
        self._clob_passphrase = clob_passphrase
        self._client: Any = None

    def is_unlocked(self) -> bool:
        return self.paper_trader.compute_stats().ready_for_live

    def daily_pnl(self) -> float:
        """Sum of resolved live PnL today (UTC date). Kill switch reads this."""
        if not self._log_path.exists():
            return 0.0
        today = datetime.now(timezone.utc).date().isoformat()
        total = 0.0
        with open(self._log_path) as f:
            for row in csv.DictReader(f):
                if row.get("submitted_at", "").startswith(today) and row.get("pnl_usd"):
                    try:
                        total += float(row["pnl_usd"])
                    except ValueError:
                        pass
        return total

    def kelly_size_usd(self, signal: Signal) -> float:
        """Quarter-Kelly stake in USD, capped at MAX_LIVE_TRADE_USD."""
        ep = signal.entry_price
        if not (0.0 < ep < 1.0):
            return 0.0
        b = (1.0 / ep) - 1.0
        p = signal.model_p if signal.direction == "YES" else (1.0 - signal.model_p)
        full_kelly = (b * p - (1.0 - p)) / b
        if full_kelly <= 0:
            return 0.0
        return min(self.bankroll_usd * KELLY_FRACTION * full_kelly, _get_max_trade_usd())

    def execute_signal(self, signal: Signal) -> dict | None:
        """
        Place a limit order for signal. Returns order info dict or None if skipped.
        Raises RuntimeError on hard blocks (gates not passed, kill switch, bad creds).
        """
        if not self.is_unlocked():
            raise RuntimeError("Go-live gates not passed — run: python weather_bot.py dashboard")

        # Daily loss kill switch — live now that daily_pnl reads real pnl_usd
        today_pnl = self.daily_pnl()
        if today_pnl < -(self.bankroll_usd * DAILY_LOSS_LIMIT_PCT):
            raise RuntimeError(
                f"Daily loss limit hit: {today_pnl:.2f} USD — halting until tomorrow"
            )

        if self._is_duplicate(signal):
            return None

        size_usd = self.kelly_size_usd(signal)

        # Pre-trade balance guard — never size above currently-available USDC.
        # Re-fetches per trade so cumulative sizing across multiple signals in
        # one run can't exceed real funds. Skipped when no private key is set
        # (unit tests inject a mock _poly directly, without credentials).
        if self._private_key:
            try:
                size_usd = min(size_usd, self.fetch_balance())
            except Exception:
                pass  # balance unavailable — proceed on the Kelly size

        assert size_usd <= _get_max_trade_usd(), f"kelly_size_usd exceeded cap: {size_usd}"
        if size_usd < 1.0:
            return None

        from py_clob_client_v2.clob_types import OrderArgsV2, OrderType, PartialCreateOrderOptions
        from py_clob_client_v2.order_builder.constants import BUY

        client = self._get_client()
        ep = signal.entry_price
        n_contracts = round(size_usd / ep, 2)

        # Enforce the CLOB minimum order size (in contracts). An undersized order
        # is rejected by the book ("breaks minimum" 400), so bump up to the floor
        # when the resulting stake stays within the per-trade cap; otherwise skip.
        min_size = signal.market.min_order_size
        if min_size > 0 and n_contracts < min_size:
            bumped_usd = min_size * ep
            if bumped_usd <= _get_max_trade_usd():
                n_contracts = round(min_size, 2)
                size_usd = bumped_usd
            else:
                return None

        # Resolve the CLOB token ID for the direction we're trading
        token_id = (
            signal.market.yes_token_id
            if signal.direction == "YES"
            else signal.market.no_token_id
        )
        if not token_id:
            # Fallback: look up from CLOB API (market wasn't scanned with token IDs)
            market_data = client.get_market(signal.market.market_id)
            tokens = market_data.get("tokens", [])
            for t in tokens:
                if t.get("outcome", "").upper() == signal.direction:
                    token_id = t.get("token_id", "")
                    break
        if not token_id:
            raise RuntimeError(
                f"No CLOB token_id for {signal.direction} on {signal.market.market_id}. "
                "Re-scan to pick up clobTokenIds from Gamma API."
            )

        tick_size = signal.market.tick_size or "0.01"

        # Reserve the idempotency key BEFORE submitting: if the process dies
        # between submit and the post-write, the reservation still blocks a
        # duplicate order on the next run. Roll back if submit itself fails.
        self._write_idempotency_key(signal, "pending")
        try:
            # FAK (Fill-And-Kill): fill whatever depth is available at/inside our
            # limit price immediately, kill the remainder. Avoids GTC orders
            # resting unfilled and getting picked off later at a stale weather
            # edge. Partial fills are fine — reconciled from size_matched below.
            result = client.create_and_post_order(
                OrderArgsV2(token_id=token_id, price=ep, size=n_contracts, side=BUY),
                options=PartialCreateOrderOptions(
                    tick_size=tick_size, neg_risk=signal.market.neg_risk
                ),
                order_type=OrderType.FAK,
            )
        except Exception:
            self._remove_idempotency_key(signal)
            raise
        order_id = result.get("orderID") or result.get("id") or str(result)

        self._write_idempotency_key(signal, order_id)

        if self._fill_poll_delay > 0:
            time.sleep(self._fill_poll_delay)
        order_obj = client.get_order(order_id)
        # size_matched is 6-decimal fixed-math per the CLOB OpenOrder schema
        # ("100000000" == 100 contracts); py-clob-client-v2 returns the raw API
        # JSON unmodified, so normalise here. price is a plain decimal — leave it.
        raw_matched = order_obj.get("size_matched", order_obj.get("filled", 0)) or 0
        filled = float(raw_matched) / 1e6
        filled_price = float(order_obj.get("price", ep) or ep)
        order_status = str(order_obj.get("status", "unknown"))

        # A fill can never exceed what we ordered. If this trips, the fixed-math
        # scaling assumption above is wrong (API changed) — fail loud rather than
        # log a 1e6x-inflated size into the P&L.
        assert filled <= n_contracts * 1.01, (
            f"fill {filled} exceeds order size {n_contracts} — check size_matched scaling"
        )

        if filled <= 0:
            return {"order_id": order_id, "status": "unfilled", "filled": 0.0}

        self._log_trade(signal, order_id, size_usd, ep, filled, filled_price, order_status)
        self.paper_trader.log_trade(signal)

        return {
            "order_id": order_id,
            "size_usd": size_usd,
            "n_contracts": n_contracts,
            "filled": filled,
            "filled_price": filled_price,
            "price": filled_price,  # alias used by weather_bot.py display
            "status": order_status,
        }

    def auto_resolve(self, weather_client, model=None) -> tuple[int, int]:
        """
        Fetch actual outcomes and resolve unresolved live trades.
        Respects location_tz stored per-trade; falls back to UTC for pre-fix rows.
        Returns (resolved_count, skipped_count).
        """
        if not self._log_path.exists():
            return 0, 0

        now = datetime.now(timezone.utc)
        with open(self._log_path) as f:
            trades = list(csv.DictReader(f))
        resolved = skipped = 0

        for t in trades:
            if t.get("actual_outcome") in ("0", "1", 0, 1):
                continue

            res_date_str = t.get("resolution_date", "")
            if not res_date_str:
                skipped += 1
                continue
            res_dt = datetime.fromisoformat(res_date_str)
            if not res_dt.tzinfo:
                res_dt = res_dt.replace(tzinfo=timezone.utc)
            if res_dt > now:
                continue

            metric = t.get("metric", "")
            lat_str = t.get("lat", "")
            lon_str = t.get("lon", "")
            if not metric or not lat_str or not lon_str:
                skipped += 1
                continue

            try:
                lat, lon = float(lat_str), float(lon_str)
                threshold = float(t["threshold"])
                threshold_high = float(t["threshold_high"]) if t.get("threshold_high") else None
                w_dir = t.get("weather_direction", "above")
                filled_size = float(t.get("filled_size", 0) or 0)
                filled_price = float(t.get("filled_price", 0) or 0)
            except (ValueError, KeyError):
                skipped += 1
                continue

            if filled_size <= 0:
                skipped += 1
                continue

            loc_tz = t.get("location_tz") or "UTC"
            loc = Location(city="", lat=lat, lon=lon, timezone=loc_tz)
            actual_val = weather_client.get_historical_actual(loc, res_dt.date(), metric)
            if actual_val is None:
                skipped += 1
                continue

            outcome = _evaluate_outcome(actual_val, threshold, w_dir, threshold_high)

            # PnL: filled_size is contracts; filled_price is cost per contract
            if t.get("direction") == "YES":
                pnl = filled_size * ((1.0 - filled_price) if outcome else -filled_price)
            else:
                pnl = filled_size * ((1.0 - filled_price) if (not outcome) else -filled_price)

            model_p = float(t.get("model_p", 0.5) or 0.5)
            t["actual_outcome"] = int(outcome)
            t["resolved_at"] = now.isoformat()
            t["pnl_usd"] = round(pnl, 4)
            t["brier_score"] = round(_brier(model_p, outcome), 4)
            eur_rate = _fetch_ecb_rate(res_dt.date())
            if eur_rate is not None:
                t["eur_rate"] = eur_rate
                t["pnl_eur"] = round(pnl * eur_rate, 4)
            resolved += 1

            if model is not None:
                model.log_observation(model_p, outcome, direction=w_dir)

        if resolved:
            atomic_write_csv(self._log_path, _CSV_HEADERS, trades)

        return resolved, skipped

    def fetch_balance(self) -> float:
        """Return available USDC balance (dollars) via the official CLOB SDK."""
        return _read_collateral_balance(self._get_client())

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _idempotency_key(signal: Signal) -> str:
        return f"{signal.market.market_id}:{signal.direction}:{date.today().isoformat()}"

    def _is_duplicate(self, signal: Signal) -> bool:
        """Return True if this market+direction already has an open/unresolved trade."""
        key = self._idempotency_key(signal)

        if self._idempotency_path.exists():
            try:
                keys = json.loads(self._idempotency_path.read_text())
                if key in keys:
                    return True
            except (json.JSONDecodeError, OSError):
                pass

        if self._log_path.exists():
            with open(self._log_path) as f:
                for row in csv.DictReader(f):
                    if (
                        row.get("market_id") == signal.market.market_id
                        and row.get("direction") == signal.direction
                        and row.get("actual_outcome") in (None, "")
                    ):
                        return True

        return False

    def _write_idempotency_key(self, signal: Signal, order_id: str) -> None:
        key = self._idempotency_key(signal)
        try:
            keys = json.loads(self._idempotency_path.read_text()) if self._idempotency_path.exists() else {}
            keys[key] = order_id
            self._idempotency_path.parent.mkdir(exist_ok=True)
            atomic_write_json(self._idempotency_path, keys)
        except (OSError, json.JSONDecodeError):
            pass

    def _remove_idempotency_key(self, signal: Signal) -> None:
        """Roll back a reserved idempotency key (best-effort) when submit fails."""
        key = self._idempotency_key(signal)
        try:
            if not self._idempotency_path.exists():
                return
            keys = json.loads(self._idempotency_path.read_text())
            if keys.pop(key, None) is not None:
                atomic_write_json(self._idempotency_path, keys)
        except (OSError, json.JSONDecodeError):
            pass

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        pk = self._private_key
        if not pk or pk in ("0x...", ""):
            raise RuntimeError(
                "No private key — pass credentials via LiveTrader constructor"
            )
        self._client = _make_clob_client(
            pk=pk,
            funder_address=self._funder_address,
            signature_type=self._signature_type,
            clob_api_key=self._clob_api_key,
            clob_secret=self._clob_secret,
            clob_passphrase=self._clob_passphrase,
        )
        return self._client

    def _log_trade(
        self,
        signal: Signal,
        order_id: str,
        size_usd: float,
        entry_price: float,
        filled_size: float,
        filled_price: float,
        order_status: str,
    ) -> None:
        is_new = not self._log_path.exists()
        self._log_path.parent.mkdir(exist_ok=True)
        with open(self._log_path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=_CSV_HEADERS, extrasaction="ignore")
            if is_new:
                writer.writeheader()
            writer.writerow({
                "trade_id":          signal.market.market_id[:8],
                "market_id":         signal.market.market_id,
                "market_title":      signal.market.title[:80],
                "order_id":          order_id,
                "signal_time":       signal.signal_time.isoformat(),
                "direction":         signal.direction,
                "entry_price":       round(entry_price, 4),
                "model_p":           round(signal.model_p, 4),
                "size_usd":          round(size_usd, 2),
                "kelly_fraction":    round(KELLY_FRACTION, 3),
                "edge_pp":           round(signal.edge_pp, 4),
                "filled_size":       round(filled_size, 4),
                "filled_price":      round(filled_price, 4),
                "order_status":      order_status,
                "submitted_at":      datetime.now(timezone.utc).isoformat(),
                "error":             "",
                "resolution_date":   signal.market.resolution_date.isoformat(),
                "metric":            signal.market.metric,
                "threshold":         signal.market.threshold,
                "threshold_high":    signal.market.threshold_high if signal.market.threshold_high is not None else "",
                "weather_direction": signal.market.direction,
                "lat":               signal.market.location.lat,
                "lon":               signal.market.location.lon,
                "location_tz":       signal.market.location.timezone or "UTC",
                "actual_outcome":    "",
                "resolved_at":       "",
                "pnl_usd":           "",
                "brier_score":       "",
                "eur_rate":          "",
                "pnl_eur":           "",
            })
