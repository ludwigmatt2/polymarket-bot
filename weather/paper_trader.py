"""
Paper trading engine — log hypothetical trades, track Brier scores, enforce go-live gate.

No real orders are placed here. The paper trader is the validation layer that must
run for 4-6 weeks (20+ resolved trades) before live trading is unlocked.
"""

from __future__ import annotations

import csv
import json
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ._io import atomic_write_csv
from .config import (
    DAILY_LOSS_LIMIT_PCT,
    MIN_BRIER_SKILL_SCORE,
    MIN_PROFIT_FACTOR,
    MIN_RESOLVED_TRADES,
    MAX_PAPER_DRAWDOWN_PCT,
    PAPER_TRADE_SIZE_USD,
)
from .models import Location, PaperTrade, PaperTradingStats, Signal

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
        )
        self._append_trade(trade)
        self._existing_keys.add(key)
        self._check_milestone()
        return trade

    def _check_milestone(self, interval: int = 100) -> None:
        """Fire a macOS notification when total logged trades crosses a multiple of interval."""
        count = sum(1 for _ in self._load_all())
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
            loc = Location(city="", lat=lat, lon=lon, timezone=loc_tz)
            actual_val = weather_client.get_historical_actual(loc, res_dt.date(), metric)
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

        # Go-live gate evaluation
        failure_reasons = []
        if n_resolved < MIN_RESOLVED_TRADES:
            failure_reasons.append(f"need_{MIN_RESOLVED_TRADES}_resolved_have_{n_resolved}")
        if profit_factor < MIN_PROFIT_FACTOR:
            failure_reasons.append(f"profit_factor_{profit_factor:.2f}_below_{MIN_PROFIT_FACTOR}")
        if bss < MIN_BRIER_SKILL_SCORE:
            failure_reasons.append(f"bss_{bss:.3f}_below_{MIN_BRIER_SKILL_SCORE}")
        if max_dd > MAX_PAPER_DRAWDOWN_PCT:
            failure_reasons.append(f"drawdown_{max_dd:.1%}_above_{MAX_PAPER_DRAWDOWN_PCT:.0%}")

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
        )

    def print_dashboard(self) -> None:
        stats = self.compute_stats()
        print("\n══════════════════ Paper Trading Dashboard ══════════════════")
        print(f"  Total trades:      {stats.total_trades}")
        print(f"  Resolved:          {stats.resolved_trades}")
        print(f"  Win rate:          {stats.win_rate:.1%}")
        print(f"  Profit factor:     {stats.profit_factor:.2f}  (need ≥ {MIN_PROFIT_FACTOR})")
        print(f"  Brier Skill Score: {stats.brier_skill_score:+.3f}  (need ≥ {MIN_BRIER_SKILL_SCORE})")
        print(f"  Mean Brier:        {stats.mean_brier_score:.4f}  (0.25 = random)")
        print(f"  Total paper PnL:   €{stats.total_paper_pnl:.2f}")
        print(f"  Avg edge:          {stats.avg_edge_pp:.1%}")
        print(f"  Max drawdown:      {stats.max_drawdown_pct:.1%}  (limit: {MAX_PAPER_DRAWDOWN_PCT:.0%})")
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


def _evaluate_outcome(
    actual: float,
    threshold: float,
    direction: str,
    threshold_high: float | None = None,
) -> bool:
    if direction == "above":
        return actual > threshold
    if direction == "below":
        return actual < threshold
    if direction == "range" and threshold_high is not None:
        return threshold <= actual <= threshold_high
    return abs(actual - threshold) <= 0.5  # "equal" — ±0.5°C
