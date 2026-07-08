"""
Mark-to-model position monitor.

After each scan, re-evaluates all open paper trades against the latest
ensemble forecast. Flags positions where:
  - The updated model_p has flipped direction vs the entry direction
  - The updated edge has shrunk below MIN_NET_EV_PP (trade no longer justifies holding)
  - The updated model_p has moved by more than FLIP_THRESHOLD pp

Writes flags to logs/position_flags.csv and prints a summary.
In live trading this would trigger exits; in paper trading it surfaces
informational alerts.
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

from .city_bias import CityBiasCorrector
from .config import MIN_NET_EV_PP, EDGE_SAFETY_MARGIN_PP
from .models import Location, WeatherMarket
from .probability_model import ProbabilityModel
from .weather_client import WeatherClient

FLAGS_CSV = Path("logs/position_flags.csv")
FLIP_THRESHOLD = 0.15      # flag if model_p moved >15pp since entry
MIN_RELIABLE_MEMBERS = 10  # skip re-evaluation if ensemble is sparse


class PositionMonitor:
    def __init__(
        self,
        client: WeatherClient,
        model: ProbabilityModel,
        trades_csv: Path = Path("logs/paper_trades.csv"),
        bias_corrector: CityBiasCorrector | None = None,
    ):
        self.client = client
        self.model  = model
        self.trades_csv = trades_csv
        self.bias_corrector = bias_corrector or CityBiasCorrector()

    def check_open_positions(self) -> list[dict]:
        """
        Re-evaluate all open positions. Returns list of flagged positions.
        """
        open_trades = self._load_open_trades()
        if not open_trades:
            return []

        now = datetime.now(timezone.utc)
        flags: list[dict] = []

        for t in open_trades:
            try:
                lat         = float(t["lat"])
                lon         = float(t["lon"])
                threshold   = float(t["threshold"])
                threshold_h = float(t["threshold_high"]) if t.get("threshold_high") else None
                w_dir       = t["weather_direction"]
                trade_dir   = t["direction"]
                entry_p     = float(t["entry_price"])
                orig_model_p = float(t["model_p"])
                res_dt      = datetime.fromisoformat(t["resolution_date"])
                if not res_dt.tzinfo:
                    res_dt = res_dt.replace(tzinfo=timezone.utc)
                market_title = t["market_title"]
                metric       = t["metric"]
            except (ValueError, KeyError):
                continue

            days_to_res = (res_dt - now).total_seconds() / 86400
            if days_to_res < 0:
                continue  # already past resolution

            loc = Location(city="", lat=lat, lon=lon, timezone="UTC")
            try:
                forecast = self.client.get_ensemble_forecast(loc, res_dt.date(), metric)
            except Exception:
                continue

            if len(forecast.all_members) < MIN_RELIABLE_MEMBERS:
                continue

            # Apply city bias correction
            bias = self.bias_corrector.get_offset(lat, lon)
            adj_threshold   = threshold - bias
            adj_threshold_h = (threshold_h - bias) if threshold_h is not None else None

            prob = self.model.compute_probability(
                forecast=forecast,
                threshold=adj_threshold,
                direction=w_dir,
                threshold_high=adj_threshold_h,
            )

            updated_model_p = prob.calibrated_p
            updated_dir     = "YES" if updated_model_p > (1 - entry_p if trade_dir == "NO" else entry_p) else "NO"
            mkt_yes_price   = entry_p if trade_dir == "YES" else (1 - entry_p)
            current_edge    = abs(updated_model_p - mkt_yes_price) - EDGE_SAFETY_MARGIN_PP
            p_shift         = abs(updated_model_p - orig_model_p)
            direction_flipped = updated_dir != trade_dir

            reasons = []
            if direction_flipped:
                reasons.append("direction_flipped")
            if current_edge < MIN_NET_EV_PP:
                reasons.append(f"edge_gone:{current_edge:.3f}")
            if p_shift > FLIP_THRESHOLD:
                reasons.append(f"large_shift:{p_shift:.2f}")

            if reasons:
                flag = {
                    "flagged_at":      now.isoformat(),
                    "trade_id":        t["trade_id"],
                    "market_title":    market_title[:60],
                    "resolution_date": res_dt.date().isoformat(),
                    "trade_dir":       trade_dir,
                    "orig_model_p":    round(orig_model_p, 3),
                    "updated_model_p": round(updated_model_p, 3),
                    "p_shift":         round(p_shift, 3),
                    "current_edge":    round(current_edge, 3),
                    "direction_flipped": int(direction_flipped),
                    "reasons":         "|".join(reasons),
                }
                flags.append(flag)

        if flags:
            self._append_flags(flags)

        return flags

    def _load_open_trades(self) -> list[dict]:
        if not self.trades_csv.exists():
            return []
        rows = list(csv.DictReader(open(self.trades_csv)))
        return [r for r in rows
                if r.get("actual_outcome") in (None, "", "None")
                and r.get("metric") and r.get("lat") and r.get("lon")]

    def _append_flags(self, flags: list[dict]) -> None:
        is_new = not FLAGS_CSV.exists()
        FLAGS_CSV.parent.mkdir(exist_ok=True)
        with open(FLAGS_CSV, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(flags[0].keys()))
            if is_new:
                writer.writeheader()
            writer.writerows(flags)


def print_divergences(divergences: list[dict]) -> None:
    """Render on-chain reconciliation divergences from LiveTrader.reconcile_positions."""
    if not divergences:
        print("  ✅ Local trade log matches on-chain positions.")
        return
    print(f"  ⚠️  {len(divergences)} reconciliation divergence(s):")
    for d in divergences:
        mkt = str(d.get("market_id", ""))[:14]
        if d.get("type") == "missing_on_chain":
            print(f"    local-only  {d.get('direction','?')} {mkt}… "
                  f"(order {str(d.get('order_id') or '?')[:10]}) — no on-chain position")
        else:
            print(f"    on-chain-only {d.get('direction','?')} {mkt}… "
                  f"(size {d.get('size', 0)}) — no open local trade")


def print_flags(flags: list[dict]) -> None:
    if not flags:
        print("  ✅ All open positions healthy — no flags.")
        return
    print(f"  ⚠️  {len(flags)} position(s) flagged:\n")
    for f in flags:
        flip = "🔄 FLIPPED" if f["direction_flipped"] else "⚠️  WEAKENED"
        print(f"  {flip}  {f['trade_dir']} | {f['orig_model_p']:.0%} → {f['updated_model_p']:.0%} "
              f"| edge={f['current_edge']:+.3f} | {f['market_title']}")
        print(f"          reasons: {f['reasons']}  |  resolves {f['resolution_date']}")
