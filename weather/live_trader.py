"""
Live trader — Kelly-sized order execution via pmxt.

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


def _fetch_clob_balance_direct(
    address: str, api_key: str, secret: str, passphrase: str
) -> float:
    """Query CLOB /balance directly with L2 HMAC auth, bypassing pmxt sidecar.

    Used as a fallback for gnosis-safe accounts where pmxt's hosted sidecar
    returns 0 despite the account having funds.
    """
    import base64
    import hashlib
    import hmac as _hmac
    import json as _json
    import time
    import urllib.request

    ts = str(int(time.time()))
    msg = ts + "GET" + "/balance" + ""
    try:
        key_bytes = base64.b64decode(secret)
    except Exception:
        key_bytes = secret.encode()
    sig = base64.b64encode(
        _hmac.new(key_bytes, msg.encode(), hashlib.sha256).digest()
    ).decode()

    req = urllib.request.Request(
        "https://clob.polymarket.com/balance",
        headers={
            "POLY_ADDRESS": address,
            "POLY_SIGNATURE": sig,
            "POLY_TIMESTAMP": ts,
            "POLY_NONCE": "0",
            "POLY_API_KEY": api_key,
            "POLY_PASSPHRASE": passphrase,
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = _json.loads(resp.read())
    return float(data.get("balance", 0) or data.get("free", 0))


class LiveTrader:
    def __init__(
        self,
        paper_trader: PaperTrader,
        bankroll_usd: float,
        fill_poll_delay: float = 2.0,
        log_path: Path = LIVE_TRADES_LOG,
        idempotency_path: Path = _IDEMPOTENCY_FILE,
        private_key: str | None = None,
        proxy_address: str | None = None,
        signature_type: str | None = None,
        clob_api_key: str | None = None,
        clob_secret: str | None = None,
        clob_passphrase: str | None = None,
    ):
        self.paper_trader = paper_trader
        self.bankroll_usd = bankroll_usd
        self._fill_poll_delay = fill_poll_delay  # override to 0 in tests
        self._log_path = log_path
        self._idempotency_path = idempotency_path
        self._private_key = private_key
        self._proxy_address = proxy_address
        self._signature_type = signature_type
        self._clob_api_key = clob_api_key
        self._clob_secret = clob_secret
        self._clob_passphrase = clob_passphrase
        self._poly: Any = None

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
        assert size_usd <= _get_max_trade_usd(), f"kelly_size_usd exceeded cap: {size_usd}"
        if size_usd < 1.0:
            return None

        poly = self._get_poly()
        ep = signal.entry_price
        n_contracts = round(size_usd / ep, 2)

        mkt = poly.fetch_market(id=signal.market.market_id)
        outcome = mkt.yes if signal.direction == "YES" else mkt.no

        built = poly.build_order(
            market_id=signal.market.market_id,
            outcome_id=outcome.market_id,
            side="buy",
            type="limit",
            amount=n_contracts,
            price=round(ep, 4),
        )
        order = poly.submit_order(built)
        order_id = str(getattr(order, "id", order))

        self._write_idempotency_key(signal, order_id)

        if self._fill_poll_delay > 0:
            time.sleep(self._fill_poll_delay)
        order_obj = poly.fetch_order(order_id)
        filled = float(getattr(order_obj, "filled", 0) or 0)
        filled_price = float(getattr(order_obj, "price", None) or ep)
        order_status = str(getattr(order_obj, "status", "unknown"))

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
                pnl = filled_size * (filled_price if not outcome else -(1.0 - filled_price))

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
        """Return available USDC balance.

        Tries pmxt first.  If pmxt returns 0 and L2 CLOB credentials are stored
        (clob_api_key / clob_secret), falls back to a direct CLOB /balance call —
        needed for gnosis-safe accounts where pmxt's hosted sidecar returns 0.
        """
        poly = self._get_poly()
        balances = poly.fetch_balance()
        for b in balances:
            if getattr(b, "currency", "") in ("USDC", "USDC.e"):
                try:
                    val = float(getattr(b, "free", 0) or getattr(b, "total", 0))
                    if val > 0:
                        return val
                except (TypeError, ValueError):
                    pass
        # pmxt returned 0 — try direct CLOB query if L2 creds are available
        if self._clob_api_key and self._clob_secret:
            address = self._proxy_address
            if not address and self._private_key:
                from eth_account import Account
                address = Account.from_key(self._private_key).address
            if address:
                try:
                    return _fetch_clob_balance_direct(
                        address, self._clob_api_key,
                        self._clob_secret, self._clob_passphrase or "",
                    )
                except Exception:
                    pass
        return 0.0

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

        if self._poly is not None:
            try:
                positions = self._poly.fetch_positions()
                for pos in positions:
                    if (
                        getattr(pos, "market_id", None) == signal.market.market_id
                        and float(getattr(pos, "size", 0)) > 0
                    ):
                        return True
            except Exception:
                pass

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

    def _get_poly(self) -> Any:
        if self._poly is not None:
            return self._poly
        pk = self._private_key
        if not pk or pk in ("0x...", ""):
            raise RuntimeError(
                "No private key — pass credentials via LiveTrader constructor"
            )
        import pmxt
        proxy = self._proxy_address
        self._poly = pmxt.Polymarket(
            private_key=pk,
            proxy_address=proxy or None,
            signature_type=self._signature_type or ("gnosis-safe" if proxy else "eoa"),
        )
        return self._poly

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
