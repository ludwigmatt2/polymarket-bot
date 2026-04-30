"""
Paper trading engine — log hypothetical trades, track Brier scores, enforce go-live gate.

No real orders are placed here. The paper trader is the validation layer that must
run for 4-6 weeks (20+ resolved trades) before live trading is unlocked.
"""

from __future__ import annotations

import csv
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .config import (
    DAILY_LOSS_LIMIT_PCT,
    MIN_BRIER_SKILL_SCORE,
    MIN_PROFIT_FACTOR,
    MIN_RESOLVED_TRADES,
    MAX_PAPER_DRAWDOWN_PCT,
    PAPER_TRADE_SIZE_USD,
)
from .models import PaperTrade, PaperTradingStats, Signal

PAPER_TRADES_LOG = Path("logs/paper_trades.csv")

CSV_HEADERS = [
    "trade_id", "market_id", "market_title",
    "signal_time", "entry_price", "model_p", "direction",
    "size_usd", "edge_pp", "ensemble_spread", "confidence_score",
    "resolution_date",
    "actual_outcome", "resolved_at", "pnl_usd", "brier_score",
    "cumulative_pnl", "cumulative_brier",
]

# Brier score for an uninformed 50/50 forecast (climatology baseline)
_CLIMATOLOGY_BRIER = 0.25


class PaperTrader:
    def __init__(self, log_path: Path = PAPER_TRADES_LOG):
        self.log_path = log_path

    def log_trade(self, signal: Signal) -> PaperTrade | None:
        """
        Record a hypothetical trade entry. Returns the PaperTrade object.
        Returns None if the signal gate did not pass.
        """
        if not signal.quality_gate_passed:
            return None

        trade = PaperTrade(
            trade_id=str(uuid.uuid4())[:8],
            market_id=signal.market.market_id,
            market_title=signal.market.title,
            signal_time=signal.signal_time,
            entry_price=signal.market_p if signal.direction == "YES" else (1.0 - signal.market_p),
            model_p=signal.model_p,
            direction=signal.direction,
            size_usd=PAPER_TRADE_SIZE_USD,
            edge_pp=signal.edge_pp,
            ensemble_spread=signal.ensemble_spread,
            confidence_score=signal.confidence_score,
            resolution_date=signal.market.resolution_date,
        )
        self._append_trade(trade)
        return trade

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

        # PnL: if direction=YES and outcome=True → win (1-entry_price) per unit
        # if direction=YES and outcome=False → lose entry_price per unit
        if direction == "YES":
            pnl = size_usd * ((1.0 - entry_price) if actual_outcome else -entry_price)
        else:
            # Bought NO at (1-yes_price)
            pnl = size_usd * ((entry_price) if not actual_outcome else -(1.0 - entry_price))

        brier = (model_p - float(actual_outcome)) ** 2
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

    def compute_stats(self) -> PaperTradingStats:
        """Compute aggregate metrics over all resolved trades."""
        trades = self._load_all()
        resolved = [t for t in trades if t.get("actual_outcome") not in (None, "", "None")]

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

        # Max drawdown
        cumulative = 0.0
        peak = 0.0
        max_dd = 0.0
        hypothetical_capital = PAPER_TRADE_SIZE_USD * MIN_RESOLVED_TRADES  # proxy
        for p in pnls:
            cumulative += p
            if cumulative > peak:
                peak = cumulative
            dd = (peak - cumulative) / max(hypothetical_capital, 1)
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
                "edge_pp": trade.edge_pp,
                "ensemble_spread": trade.ensemble_spread,
                "confidence_score": trade.confidence_score,
                "resolution_date": trade.resolution_date.isoformat(),
                "actual_outcome": "",
                "resolved_at": "",
                "pnl_usd": "",
                "brier_score": "",
                "cumulative_pnl": "",
                "cumulative_brier": "",
            })

    def _load_all(self) -> list[dict]:
        if not self.log_path.exists():
            return []
        with open(self.log_path) as f:
            return list(csv.DictReader(f))

    def _rewrite_all(self, trades: list[dict]) -> None:
        with open(self.log_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_HEADERS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(trades)
