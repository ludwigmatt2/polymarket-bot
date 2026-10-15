"""Pre-registered gate: is live Kelly sizing larger than the realized edge supports?

Context (2026-10-15, in-sample). The model's claimed edge (edge_pp) averages
~13pp. Realized edge (win rate minus price paid) was ~+1.7pp on 211 live fills
and ~+5.9pp on 528 paper trades (entry >= 0.10). Kelly stakes scale with the
edge the model believes, so 25%-Kelly on a 13pp claim is effectively 0.5-1.9x
full Kelly on the real edge — which is what makes single losses big. Both tracks
are noisy and disagree on SHAPE (by price band and by claimed-edge bucket), so
this is not acted on: only the aggregate overstatement is consistent.

Shadow-track note: nothing here needs a ModelSpec or new logging. Paper (and
every shadow, which reuses PaperTrader) sizes FLAT (PAPER_TRADE_SIZE_USD x
size_factor) — Kelly only exists in LiveTrader.kelly_size_usd. The question is
purely "given the real fills and outcomes, what stake multiplier would have
maximized log growth?", and live_trades.csv already has everything.

THE METRIC. Replay the resolved live fills in resolved_at order. For each fill
the OBSERVED bankroll fraction is f_i = stake_i / W_i (W tracked as BANKROLL_START
+ cumulative realized pnl) and the per-$ return is r_i = pnl_i / stake_i. Under a
stake multiplier s the counterfactual bankroll compounds as
W' <- W' * (1 + s * f_i * r_i). s* is the grid multiplier with the highest final
log-wealth. s*=1 means current sizing is growth-optimal in hindsight; s*<1 means
it was too big; s*=0 means no edge was realized at all (NOT "bet less", "don't
bet" — the verdict text says so).

THE GATE (locked 2026-10-15 before scoring new data — do not loosen):
  - Only live fills with signal_time >= LOCK_DATE count.
  - Answerable only at n >= MIN_N (250) resolved fills.
  - Day-clustered bootstrap (same-day fills are correlated) of s*.
  - PROCEED (reduce sizing) iff P(s* <= S_REDUCE (0.70)) >= BOOT_P_MIN (0.80),
    i.e. we are 80% sure current stakes are >= ~1.43x the growth-optimal size.
    Recommended KELLY_FRACTION = current 0.25 x median s* (rounded to 0.01).
  - Otherwise keep sizing. A result with s* >= 1 does NOT justify going bigger
    under this gate; that is a separate, harder claim.
AMENDED 2026-10-15 (same day, before any post-lock fill had resolved, n=0): pnl
and the bankroll path are NET of the Polymarket taker fee (weather/fees.py,
0.05*p*(1-p) per share, measured on-chain; live_trades.csv pnl_usd is gross of
it). Realized edge is reported net of fee per share too. A fee of ~2.4% of
stake is as large as the whole realized edge, so a gross replay would call
sizing optimal where the net one may not.
Caveats baked in: no bankroll feedback beyond the compounding above, concurrent
open positions are treated as sequential, and BANKROLL_START is the wallet
equity measured on-chain 2026-10-15 (pUSD 76.50 + open positions 12.68).

Run: venv/bin/python scripts/kelly_scale_gate.py
"""
from __future__ import annotations

import csv
import math
import random
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from weather.config import KELLY_FRACTION  # noqa: E402
from weather.fees import fee_per_share, taker_fee_usd  # noqa: E402
from weather.paths import DATA_DIR  # noqa: E402

LIVE_LOG = DATA_DIR / "logs" / "live_trades.csv"
LOCK_DATE = "2026-10-15T00:00:00+00:00"  # do not move this once set
BANKROLL_START = 89.0
MIN_N = 250
S_REDUCE = 0.70
BOOT_P_MIN = 0.80
BOOT_N = 2000
BOOT_SEED = 1
GRID = [round(0.05 * i, 2) for i in range(0, 31)]  # 0.00 .. 1.50


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def load_fills(path: Path = LIVE_LOG) -> list[dict]:
    """Resolved filled live rows: stake, pnl, edge claim, price, times."""
    if not path.exists():
        sys.exit(f"No live_trades.csv at {path}\n"
                 "  On the VPS: sudo -u bot env RAILWAY_VOLUME_MOUNT_PATH=/opt/polymarket-bot/data \\\n"
                 "    venv/bin/python scripts/kelly_scale_gate.py")
    out = []
    with path.open() as f:
        for r in csv.DictReader(f):
            if r.get("order_status") != "matched":
                continue
            size, px, pnl = _num(r.get("filled_size")), _num(r.get("filled_price")), _num(r.get("pnl_usd"))
            if not size or not px or pnl is None or r.get("actual_outcome", "") == "":
                continue
            stake = size * px
            out.append({"stake": stake, "px": px, "pnl": pnl - taker_fee_usd(stake, px),
                        "gross_win": pnl > 0,
                        "edge": _num(r.get("edge_pp")),
                        "t": r.get("signal_time", ""), "res": r.get("resolved_at", "")})
    return out


def log_growth(rows: list[dict], s: float, w0: float = BANKROLL_START) -> float:
    """Final log-wealth under stake multiplier s. rows in resolved order."""
    w_obs, w_cf = w0, w0
    for r in rows:
        if r["stake"] <= 0 or w_obs <= 0 or w_cf <= 0:
            break
        frac = r["stake"] / w_obs
        ret = r["pnl"] / r["stake"]
        w_cf *= max(1.0 + s * frac * ret, 1e-9)
        w_obs += r["pnl"]
    return math.log(max(w_cf, 1e-12) / w0)


def best_multiplier(rows: list[dict]) -> float:
    ordered = sorted(rows, key=lambda r: r["res"])
    return max(GRID, key=lambda s: log_growth(ordered, s))


def _boot_s_star(rows: list[dict]) -> list[float]:
    by_day: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_day[r["res"][:10]].append(r)
    days = [sorted(v, key=lambda r: r["res"]) for _, v in sorted(by_day.items())]
    rng = random.Random(BOOT_SEED)
    out = []
    for _ in range(BOOT_N):
        sample = [r for d in (rng.choice(days) for _ in days) for r in d]
        out.append(max(GRID, key=lambda s: log_growth(sample, s)))
    return out


def evaluate(rows: list[dict]) -> dict:
    """Pure gate logic over rows already restricted to signal_time >= LOCK_DATE."""
    res = {"n": len(rows), "answerable": False}
    if not rows:
        return res
    n = len(rows)
    wins = sum(1 for r in rows if r["gross_win"])
    claimed = [r["edge"] for r in rows if r["edge"] is not None]
    res.update(
        s_star=best_multiplier(rows),
        # win rate - price paid - fee per share: the edge a share actually earned.
        realized_edge=wins / n - statistics.mean(r["px"] + fee_per_share(r["px"]) for r in rows),
        claimed_edge=statistics.mean(claimed) if claimed else None,
        curve={s: log_growth(sorted(rows, key=lambda r: r["res"]), s) for s in (0.25, 0.5, 0.75, 1.0, 1.25)},
    )
    if n < MIN_N:
        return res
    boots = _boot_s_star(rows)
    res.update(answerable=True,
               s_median=statistics.median(boots),
               p_reduce=sum(1 for s in boots if s <= S_REDUCE) / len(boots))
    res["proceed"] = res["p_reduce"] >= BOOT_P_MIN
    res["kelly_fraction"] = round(KELLY_FRACTION * res["s_median"], 2)
    return res


def main() -> None:
    print("Kelly-scale gate — is live sizing larger than realized edge supports?\n")
    rows = [r for r in load_fills() if r["t"] >= LOCK_DATE]
    r = evaluate(rows)
    print(f"GATE (locked {LOCK_DATE}): need n>={MIN_N} resolved live fills. So far: n={r['n']}.\n")
    if r["n"]:
        ce = f"{r['claimed_edge']*100:.1f}pp" if r.get("claimed_edge") is not None else "n/a"
        print(f"  context: claimed edge {ce}, realized edge (net of fee) {r['realized_edge']*100:+.1f}pp")
        print(f"  context: hindsight growth-optimal multiplier s* = {r['s_star']:.2f}")
        print("  context: log-growth by multiplier: " +
              "  ".join(f"{s:.2f}x={g:+.3f}" for s, g in r["curve"].items()) + "\n")
    if not r["answerable"]:
        print("VERDICT: not answerable yet. Nothing to turn on — live_trades.csv already "
              "records everything this needs. Do NOT act on the context numbers above.")
        return
    print(f"  bootstrap median s* = {r['s_median']:.2f}; "
          f"P(s* <= {S_REDUCE}) = {r['p_reduce']:.2f} (need >= {BOOT_P_MIN:.2f})")
    if r["proceed"]:
        extra = (" NOTE: s*≈0 means no realized edge at all — the honest response is "
                 "'stop live sizing', not 'size smaller'.") if r["s_median"] <= 0.05 else ""
        print(f"\nVERDICT: PROCEED — current stakes were too large for the realized edge. "
              f"Implement as an env-overridable KELLY_FRACTION ≈ {r['kelly_fraction']:.2f} "
              f"(now {KELLY_FRACTION:.2f}).{extra}")
    else:
        print("\nVERDICT: keep sizing. Over-sizing was not established out of sample.")


if __name__ == "__main__":
    main()
