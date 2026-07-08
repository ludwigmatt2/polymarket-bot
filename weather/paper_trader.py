"""
Paper trading engine — log hypothetical trades, track Brier scores, enforce go-live gate.

No real orders are placed here. The paper trader is the validation layer that must
run for 4-6 weeks (20+ resolved trades) before live trading is unlocked.
"""

from __future__ import annotations

import csv
import json
import os
import subprocess
import uuid
from datetime import datetime, time as _time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from ._io import atomic_write_csv
from .config import (
    DAILY_LOSS_LIMIT_PCT,
    GATE_MIN_DAYS_ELAPSED,
    GATE_MIN_STATION_RESOLVED,
    MIN_PROFIT_FACTOR,
    MAX_PAPER_DRAWDOWN_PCT,
    PAPER_TRADE_SIZE_USD,
)
from .models import Location, PaperTrade, PaperTradingStats, Signal, _evaluate_outcome
from .station_truth import station_outcome

PAPER_TRADES_LOG = Path("logs/paper_trades.csv")

CSV_HEADERS = [
    "trade_id", "market_id", "market_title",
    "signal_time", "entry_price", "model_p", "direction",
    "size_usd", "size_factor", "edge_pp", "ensemble_spread", "confidence_score",
    "resolution_date",
    # Resolution fields — stored at log time so auto-resolve needs no re-parsing
    "metric", "threshold", "threshold_high", "weather_direction", "lat", "lon", "location_tz",
    "actual_outcome", "resolved_at", "pnl_usd", "brier_score",
    "cumulative_pnl", "cumulative_brier",
    # Phase 0: appended at the end so pre-Phase-0 rows (which lack them) still parse —
    # DictReader fills missing trailing columns, _load_all backfills "" for them.
    "raw_p", "model_breakdown_json",
    # Phase 1: resolving station (empty on older rows → Open-Meteo resolution path).
    "station_icao", "station_country", "resolve_unit",
    # Which truth labeled this outcome: "station" (WU→IEM, what Polymarket pays on)
    # or "grid" (Open-Meteo reanalysis fallback). The re-live gate counts ONLY
    # station-labeled trades — grid labels disagreed with on-chain 33% of the time.
    "label_source",
    # Running-extreme clip: observed station max/min (°C) applied at signal time
    # ("" = feature stood down) — separates the feature's PnL contribution.
    "running_obs_c",
]

# Brier score for an uninformed 50/50 forecast (climatology baseline)
_CLIMATOLOGY_BRIER = 0.25


def _trade_pnl(size_usd: float, entry_price: float, bet_wins: bool) -> float:
    """option-b semantics: size_usd is the USD stake; win returns stake*(1/price-1)."""
    return size_usd * (1.0 / entry_price - 1.0) if bet_wins else -size_usd


def _brier(model_p: float, outcome: bool) -> float:
    return (model_p - float(outcome)) ** 2


class PaperTrader:
    def __init__(self, log_path: Path = PAPER_TRADES_LOG):
        self.log_path = log_path
        self._existing_keys: set[tuple[str, str]] | None = None

    def log_trade(self, signal: Signal) -> PaperTrade | None:
        """
        Record a hypothetical trade entry. Returns the PaperTrade object.
        Returns None if the signal gate did not pass or market already logged.
        """
        if not signal.quality_gate_passed:
            return None

        if self._existing_keys is None:
            self._existing_keys = {(r["market_id"], r["direction"]) for r in self._load_all()}

        key = (signal.market.market_id, signal.direction)
        if key in self._existing_keys:
            return None

        size_factor = getattr(signal, "size_factor", 1.0)
        trade = PaperTrade(
            trade_id=str(uuid.uuid4())[:8],
            market_id=signal.market.market_id,
            market_title=signal.market.title,
            signal_time=signal.signal_time,
            entry_price=signal.market_p if signal.direction == "YES" else (1.0 - signal.market_p),
            model_p=signal.model_p,
            direction=signal.direction,
            size_usd=round(PAPER_TRADE_SIZE_USD * size_factor, 2),
            size_factor=size_factor,
            edge_pp=signal.edge_pp,
            ensemble_spread=signal.ensemble_spread,
            confidence_score=signal.confidence_score,
            resolution_date=signal.market.resolution_date,
            metric=signal.market.metric,
            threshold=signal.market.threshold,
            threshold_high=signal.market.threshold_high,
            weather_direction=signal.market.direction,
            lat=signal.market.location.lat,
            lon=signal.market.location.lon,
            location_tz=signal.market.location.timezone or "UTC",
            raw_p=signal.prob_result.raw_p,
            model_breakdown_json=json.dumps(signal.prob_result.model_breakdown),
            station_icao=signal.market.station_icao,
            station_country=signal.market.station_country,
            running_obs_c=getattr(signal, "running_obs_c", None),
            resolve_unit=signal.market.resolve_unit,
        )
        self._append_trade(trade)
        self._existing_keys.add(key)
        self._check_milestone()
        return trade

    def _check_milestone(self, interval: int = 100) -> None:
        """Fire a macOS notification when total logged trades crosses a multiple of interval."""
        # _existing_keys already holds one entry per logged (market_id, direction) and is
        # maintained on every append — use it instead of re-reading the whole CSV each call.
        count = len(self._existing_keys or ())
        if count % interval == 0:
            msg = (
                f"Polymarket bot hit {count} paper trades. "
                "Time to review calibration and update the gates."
            )
            try:
                subprocess.run(
                    ["osascript", "-e",
                     f'display notification "{msg}" with title "Calibration Review Due"'],
                    check=False, timeout=5,
                )
            except Exception:
                pass

    def resolve_trade(self, trade_id: str, actual_outcome: bool) -> PaperTrade | None:
        """
        Mark a trade as resolved with the actual outcome.
        Updates the CSV row with PnL and Brier score.
        Returns the updated PaperTrade.
        """
        trades = self._load_all()
        target = next((t for t in trades if t["trade_id"] == trade_id), None)
        if target is None:
            return None

        entry_price = float(target["entry_price"])
        direction = target["direction"]
        size_usd = float(target["size_usd"])
        model_p = float(target["model_p"])

        bet_wins = actual_outcome if direction == "YES" else not actual_outcome
        pnl = _trade_pnl(size_usd, entry_price, bet_wins)

        brier = _brier(model_p, actual_outcome)
        now = datetime.now(timezone.utc).isoformat()

        target["actual_outcome"] = int(actual_outcome)
        target["resolved_at"] = now
        target["pnl_usd"] = round(pnl, 4)
        target["brier_score"] = round(brier, 4)

        self._rewrite_all(trades)

        return PaperTrade(
            trade_id=trade_id,
            market_id=target["market_id"],
            market_title=target["market_title"],
            signal_time=datetime.fromisoformat(target["signal_time"]),
            entry_price=entry_price,
            model_p=model_p,
            direction=direction,
            size_usd=size_usd,
            edge_pp=float(target["edge_pp"]),
            ensemble_spread=float(target["ensemble_spread"]),
            confidence_score=float(target["confidence_score"]),
            resolution_date=datetime.fromisoformat(target["resolution_date"]),
            actual_outcome=actual_outcome,
            resolved_at=datetime.now(timezone.utc),
            pnl_usd=round(pnl, 4),
            brier_score=round(brier, 4),
        )

    def auto_resolve(self, weather_client, model=None) -> tuple[int, int]:
        """
        Fetch actual outcomes from the archive API and resolve all eligible trades.

        Eligible: resolution_date has passed AND actual_outcome is blank AND
        the trade has location/threshold data stored (metric, lat, lon fields non-empty).

        Returns (resolved_count, skipped_count).
        """
        now = datetime.now(timezone.utc)
        trades = self._load_all()
        resolved = skipped = 0
        # Memoize station reads across trades sharing a station/day/metric this pass
        # (e.g. several threshold buckets for the same city/date = one WU fetch).
        station_cache: dict = {}

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
                continue  # not yet resolved

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
            except (ValueError, KeyError):
                skipped += 1
                continue

            loc_tz = t.get("location_tz") or "UTC"
            # Wait until the event's local day is over + settled, so we read the
            # final daily value, not a mid-day forecast. Fetch on the station's LOCAL
            # calendar day (resolution_date is UTC — can be the adjacent date).
            if not _settle_ready(res_dt, loc_tz, now):
                continue
            event_date = _local_event_date(res_dt, loc_tz)

            station_icao = t.get("station_icao") or ""
            resolve_unit = t.get("resolve_unit") or ""
            # Require the unit too — never guess it, or a °F market with a blank unit
            # would resolve in °C. Missing unit → fall through to the Open-Meteo path.
            use_station = (station_icao and resolve_unit
                           and metric in ("temperature_2m_max", "temperature_2m_min"))
            if use_station:
                # Phase 1: resolve on the station's Wunderground reading (WU → IEM
                # fallback), rounded to whole degrees in the market's unit — matching
                # how Polymarket actually settles.
                outcome, _src, _val = station_outcome(
                    station_icao, t.get("station_country") or "", resolve_unit,
                    event_date, metric, threshold, threshold_high, w_dir, cache=station_cache,
                )
                if outcome is None:
                    skipped += 1
                    continue
            else:
                loc = Location(city="", lat=lat, lon=lon, timezone=loc_tz)
                actual_val = weather_client.get_historical_actual(loc, event_date, metric)
                if actual_val is None:
                    skipped += 1
                    continue
                outcome = _evaluate_outcome(actual_val, threshold, w_dir, threshold_high)

            # Compute PnL and Brier in-place to avoid O(n²) load+rewrite per trade
            entry_price = float(t["entry_price"])
            size_usd = float(t["size_usd"])
            model_p = float(t["model_p"])
            bet_wins = outcome if t["direction"] == "YES" else not outcome
            pnl = _trade_pnl(size_usd, entry_price, bet_wins)

            t["actual_outcome"] = int(outcome)
            t["resolved_at"] = now.isoformat()
            t["pnl_usd"] = round(pnl, 4)
            t["brier_score"] = round(_brier(model_p, outcome), 4)
            t["label_source"] = "station" if use_station else "grid"
            resolved += 1

            # Phase 0 fix: the calibrator is applied to raw_p at inference
            # (probability_model.py: calibrated_p = _apply_calibration(raw_p, ...)),
            # so it must be TRAINED on raw_p — not the calibrated+shrunk model_p.
            # Fall back to model_p only for pre-Phase-0 rows that lack raw_p.
            if model is not None:
                raw_p = float(t.get("raw_p") or t["model_p"])
                model.log_observation(raw_p, outcome, direction=w_dir)

        if resolved:
            # Back-fill cumulative columns across all rows in chronological order
            cum_pnl = 0.0
            cum_brier = 0.0
            for t in trades:
                if t.get("pnl_usd") not in (None, ""):
                    cum_pnl += float(t["pnl_usd"])
                    t["cumulative_pnl"] = round(cum_pnl, 4)
                if t.get("brier_score") not in (None, ""):
                    cum_brier += float(t["brier_score"])
                    t["cumulative_brier"] = round(cum_brier, 4)
            self._rewrite_all(trades)

        return resolved, skipped

    def compute_stats(self) -> PaperTradingStats:
        """Compute aggregate metrics over all resolved trades."""
        trades = self._load_all()
        resolved = [t for t in trades if t.get("actual_outcome") in ("0", "1", 0, 1)]

        total = len(trades)
        n_resolved = len(resolved)

        if n_resolved == 0:
            return PaperTradingStats(
                total_trades=total, resolved_trades=0,
                win_rate=0.0, profit_factor=0.0,
                mean_brier_score=_CLIMATOLOGY_BRIER, brier_skill_score=0.0,
                total_paper_pnl=0.0, avg_edge_pp=0.0,
                max_drawdown_pct=0.0, ready_for_live=False,
                failure_reasons=["no_resolved_trades"],
            )

        pnls = [float(t["pnl_usd"]) for t in resolved if t.get("pnl_usd") not in (None, "")]
        briers = [float(t["brier_score"]) for t in resolved if t.get("brier_score") not in (None, "")]
        edges = [float(t["edge_pp"]) for t in resolved]

        wins = [p for p in pnls if p > 0]
        losses = [abs(p) for p in pnls if p < 0]
        profit_factor = sum(wins) / sum(losses) if losses else float("inf")
        win_rate = len(wins) / len(pnls) if pnls else 0.0
        mean_brier = sum(briers) / len(briers) if briers else _CLIMATOLOGY_BRIER
        bss = 1.0 - (mean_brier / _CLIMATOLOGY_BRIER)

        # Max drawdown — equity-curve (peak−trough)/total_capital_staked.
        # total_capital = sum of USD stakes (size_usd, option-b semantics).
        sizes = [float(t["size_usd"]) for t in resolved if t.get("size_usd") not in (None, "")]
        total_capital = max(sum(sizes), 1.0)
        equity = 0.0
        peak = 0.0
        max_dd = 0.0
        for p in pnls:
            equity += p
            peak = max(peak, equity)
            dd = (peak - equity) / total_capital
            if dd > max_dd:
                max_dd = dd

        # ── Re-live gate (Jul-8 redesign) ─────────────────────────────────────
        # Counts ONLY station-labeled trades: evidence from the fixed system,
        # scored on the thermometer Polymarket pays on. The skill test is the
        # honest one — the model's Brier must beat the MARKET PRICE's Brier on
        # the same trades (climatology is a strawman; edge means beating the
        # crowd). Plus a calendar-span floor so one weather regime can't
        # flatter the record.
        station = [t for t in resolved if t.get("label_source") == "station"]
        st_pnls, st_model_sq, st_market_sq, st_sizes = [], [], [], []
        first_signal = last_signal = None
        for t in station:
            try:
                outcome = float(t["actual_outcome"])
                ep = float(t["entry_price"])
                st_pnls.append(float(t["pnl_usd"]))
                st_sizes.append(float(t["size_usd"]))
                st_model_sq.append((float(t["model_p"]) - outcome) ** 2)
                # The market's own forecast is the YES price at entry: for a NO
                # trade entry_price bought the NO side, so P(YES) = 1 − entry.
                market_p = ep if t.get("direction") == "YES" else 1.0 - ep
                st_market_sq.append((market_p - outcome) ** 2)
                sig = str(t.get("signal_time", ""))[:10]
                if sig:
                    first_signal = min(first_signal or sig, sig)
                    last_signal = max(last_signal or sig, sig)
            except (KeyError, ValueError, TypeError):
                continue
        n_station = len(st_pnls)
        st_wins = sum(p for p in st_pnls if p > 0)
        st_losses = sum(-p for p in st_pnls if p < 0)
        st_pf = (st_wins / st_losses) if st_losses else (float("inf") if st_wins else 0.0)
        st_model_brier = sum(st_model_sq) / n_station if n_station else _CLIMATOLOGY_BRIER
        st_market_brier = sum(st_market_sq) / n_station if n_station else _CLIMATOLOGY_BRIER
        days_elapsed = 0
        if first_signal and last_signal:
            from datetime import date as _date
            days_elapsed = (_date.fromisoformat(last_signal) - _date.fromisoformat(first_signal)).days + 1
        # station-subset drawdown (equity curve over station pnls)
        st_capital = max(sum(st_sizes), 1.0)
        eq = pk = st_dd = 0.0
        for p in st_pnls:
            eq += p
            pk = max(pk, eq)
            st_dd = max(st_dd, (pk - eq) / st_capital)

        failure_reasons = []
        if n_station < GATE_MIN_STATION_RESOLVED:
            failure_reasons.append(f"need_{GATE_MIN_STATION_RESOLVED}_station_resolved_have_{n_station}")
        if days_elapsed < GATE_MIN_DAYS_ELAPSED:
            failure_reasons.append(f"need_{GATE_MIN_DAYS_ELAPSED}_days_have_{days_elapsed}")
        if st_pf < MIN_PROFIT_FACTOR:
            failure_reasons.append(f"station_profit_factor_{st_pf:.2f}_below_{MIN_PROFIT_FACTOR}")
        if st_model_brier >= st_market_brier:
            failure_reasons.append(
                f"model_brier_{st_model_brier:.4f}_not_below_market_{st_market_brier:.4f}")
        if st_dd > MAX_PAPER_DRAWDOWN_PCT:
            failure_reasons.append(f"station_drawdown_{st_dd:.1%}_above_{MAX_PAPER_DRAWDOWN_PCT:.0%}")

        return PaperTradingStats(
            total_trades=total,
            resolved_trades=n_resolved,
            win_rate=round(win_rate, 3),
            profit_factor=round(profit_factor, 3),
            mean_brier_score=round(mean_brier, 4),
            brier_skill_score=round(bss, 4),
            total_paper_pnl=round(sum(pnls), 2),
            avg_edge_pp=round(sum(edges) / len(edges), 4),
            max_drawdown_pct=round(max_dd, 4),
            ready_for_live=len(failure_reasons) == 0,
            failure_reasons=failure_reasons,
            station_resolved=n_station,
            station_profit_factor=round(st_pf, 3) if st_pf != float("inf") else st_pf,
            station_model_brier=round(st_model_brier, 4),
            station_market_brier=round(st_market_brier, 4),
            days_elapsed=days_elapsed,
        )

    def print_dashboard(self) -> None:
        stats = self.compute_stats()
        print("\n══════════════════ Paper Trading Dashboard ══════════════════")
        print(f"  Total trades:      {stats.total_trades}")
        print(f"  Resolved:          {stats.resolved_trades}")
        print(f"  Win rate:          {stats.win_rate:.1%}")
        print(f"  Profit factor:     {stats.profit_factor:.2f}")
        print(f"  Brier Skill Score: {stats.brier_skill_score:+.3f}  (vs climatology; display only)")
        print(f"  Mean Brier:        {stats.mean_brier_score:.4f}  (0.25 = random)")
        print(f"  Total paper PnL:   ${stats.total_paper_pnl:.2f}")
        print(f"  Avg edge:          {stats.avg_edge_pp:.1%}")
        print(f"  Max drawdown:      {stats.max_drawdown_pct:.1%}  (limit: {MAX_PAPER_DRAWDOWN_PCT:.0%})")
        print()
        print("  ── Re-live gate (station-labeled trades only) ──")
        print(f"  Station resolved:  {stats.station_resolved}  (need ≥ {GATE_MIN_STATION_RESOLVED})")
        print(f"  Days elapsed:      {stats.days_elapsed}  (need ≥ {GATE_MIN_DAYS_ELAPSED})")
        print(f"  Station PF:        {stats.station_profit_factor:.2f}  (need ≥ {MIN_PROFIT_FACTOR})")
        print(f"  Model vs market:   Brier {stats.station_model_brier:.4f} vs {stats.station_market_brier:.4f}"
              f"  ({'model BEATS market' if stats.station_model_brier < stats.station_market_brier else 'model does NOT beat market'})")
        print()
        if stats.ready_for_live:
            print("  ✓ ALL GATES PASSED — ready for live trading")
        else:
            print("  ✗ NOT ready for live:")
            for reason in stats.failure_reasons:
                print(f"    · {reason}")
        print("══════════════════════════════════════════════════════════════\n")

    def _append_trade(self, trade: PaperTrade) -> None:
        is_new = not self.log_path.exists()
        self.log_path.parent.mkdir(exist_ok=True)
        with open(self.log_path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_HEADERS, extrasaction="ignore")
            if is_new:
                writer.writeheader()
            writer.writerow({
                "trade_id": trade.trade_id,
                "market_id": trade.market_id,
                "market_title": trade.market_title,
                "signal_time": trade.signal_time.isoformat(),
                "entry_price": trade.entry_price,
                "model_p": trade.model_p,
                "direction": trade.direction,
                "size_usd": trade.size_usd,
                "size_factor": trade.size_factor,
                "edge_pp": trade.edge_pp,
                "ensemble_spread": trade.ensemble_spread,
                "confidence_score": trade.confidence_score,
                "resolution_date": trade.resolution_date.isoformat(),
                "metric": trade.metric,
                "threshold": trade.threshold,
                "threshold_high": trade.threshold_high if trade.threshold_high is not None else "",
                "weather_direction": trade.weather_direction,
                "lat": trade.lat,
                "lon": trade.lon,
                "location_tz": trade.location_tz,
                "actual_outcome": "",
                "resolved_at": "",
                "pnl_usd": "",
                "brier_score": "",
                "cumulative_pnl": "",
                "cumulative_brier": "",
                "raw_p": trade.raw_p,
                "model_breakdown_json": trade.model_breakdown_json,
                "station_icao": trade.station_icao,
                "station_country": trade.station_country,
                "resolve_unit": trade.resolve_unit,
                "running_obs_c": ("" if trade.running_obs_c is None
                                  else round(trade.running_obs_c, 2)),
            })

    def _load_all(self) -> list[dict]:
        if not self.log_path.exists():
            return []
        with open(self.log_path) as f:
            rows = list(csv.DictReader(f))
        if rows:
            expected = set(CSV_HEADERS)
            for row in rows:
                for field in expected - row.keys():
                    row[field] = ""
        return rows

    def _rewrite_all(self, trades: list[dict]) -> None:
        atomic_write_csv(self.log_path, CSV_HEADERS, trades)


# How long after an event's LOCAL day ends before the archive's settled daily
# max/min is trusted. Guards against resolving off an in-progress same-day
# forecast (which mismarked 3 of 8 live trades on Jul 4). Override via env.
RESOLVE_SETTLE_BUFFER_HOURS = float(os.environ.get("RESOLVE_SETTLE_BUFFER_HOURS", "6"))


def _local_event_date(res_dt: datetime, loc_tz: str):
    """The event's calendar date in the station's LOCAL timezone — the day the
    market resolves on. resolution_date is stored in UTC, which can differ from the
    local date near UTC midnight, so both the settle gate and the value fetch must
    use this, not res_dt.date()."""
    try:
        tz = ZoneInfo(loc_tz)
    except Exception:
        tz = timezone.utc
    return res_dt.astimezone(tz).date()


def _settle_ready(res_dt: datetime, loc_tz: str, now: datetime) -> bool:
    """True once the event's local day has fully ended (+ a settle buffer), so the
    archive returns the settled daily value rather than an in-progress forecast."""
    try:
        tz = ZoneInfo(loc_tz)
    except Exception:
        tz = timezone.utc
    local_midnight_after = datetime.combine(
        _local_event_date(res_dt, loc_tz) + timedelta(days=1), _time(0, 0), tzinfo=tz
    )
    deadline = local_midnight_after.astimezone(timezone.utc) + timedelta(
        hours=RESOLVE_SETTLE_BUFFER_HOURS
    )
    return now >= deadline


# _evaluate_outcome now lives in weather.models (shared with the station resolver);
# re-exported above via the models import so existing callers keep working.
