"""Pre-registered gate: should live skip NO entries priced >= 0.40?

Context (2026-10-30 backtest, in-sample, since 2026-08-20, ex-min-temp, GROSS of
fee). Live and paper agree on the shape of NO by entry price:

    NO band      live n / per-$      paper n / per-$
    0.20-0.40     22 / +45%            49 / +35%
    0.40-0.50     68 / -15%            87 /  +6%
    0.50-0.65     99 /  +5%           119 / +1.8%
    >=0.65        22 / -43%            16 / -16%   (already blocked, LIVE_MAX_ENTRY_PRICE)

Cheap NO pays; NO at 0.40-0.65 earns ~0 gross, which is net-negative after the
~2.5% taker fee (0.05*p*(1-p) per share). That band is ~167 of 211 live NO
fills, so the lever is large. But it is one in-sample read with the cut point
chosen after seeing the data (live n=68 in the worst bin; paper's 0.40-0.50 bin
is +6%, not negative). Per this project's operating rule (no model/sizing
decision under ~250 resolved trades; segment by price band) it is NOT acted on.
This script is the pre-registered verdict gate, same shape as
high_price_cap_gate.py.

Why a script and not a ModelSpec shadow track: a shadow track re-scores the
MODEL ("execution/sizing is never part of a shadow track", weather/shadow.py),
and an entry-price filter is a deterministic function of what live_trades.csv
already records (direction, filled_price, pnl_usd). Dropping those fills is
exactly the counterfactual, so nothing needs deploying to collect evidence —
this only has to refuse to answer early.

THE GATE (locked 2026-10-30 before scoring any post-lock data — do not loosen
these numbers after seeing a result):
  - Only live fills with signal_time >= LOCK_DATE count. The in-sample trades
    above are CONTEXT ONLY.
  - PRIMARY variant, fixed: drop NO fills with NO_CUT <= filled_price. NO_CUT
    is 0.40. The 0.50 cut is printed as context and never decides anything.
  - Dropping a set of fills changes PnL by minus that set's PnL, so the cut
    helps iff the dropped set's NET PnL is negative.
  - Answerable only when n >= MIN_N (250) resolved live fills since LOCK_DATE
    AND >= MIN_DROP_N (100) of them are in the dropped set (NO, px >= cut).
  - PROCEED only if the dropped set's net PnL < 0 AND a day-clustered bootstrap
    (same-day fills are correlated) says it is < 0 in >= BOOT_P_MIN (80%) of
    resamples AND it is < 0 in both halves of the window.
  - PnL is NET of the Polymarket taker fee (weather/fees.py); live_trades.csv's
    pnl_usd is gross of it. The in-sample numbers above are gross, context only.
  - No bankroll feedback: skipping a trade is assumed not to change any other.

Run: venv/bin/python scripts/no_band_gate.py
"""
from __future__ import annotations

import csv
import random
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from weather.fees import taker_fee_usd  # noqa: E402
from weather.paths import DATA_DIR  # noqa: E402

LIVE_LOG = DATA_DIR / "logs" / "live_trades.csv"
LOCK_DATE = "2026-10-30T13:00:00+00:00"  # do not move this once set
NO_CUT = 0.40         # PRIMARY: drop NO fills priced at or above this
CONTEXT_CUTS = (0.50,)
MIN_N = 250
MIN_DROP_N = 100
BOOT_P_MIN = 0.80
BOOT_N = 5000
BOOT_SEED = 1


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def load_fills(path: Path = LIVE_LOG) -> list[dict]:
    """Resolved, actually-filled live rows as {dir, px, pnl, t}. pnl is NET of
    the taker fee."""
    if not path.exists():
        sys.exit(f"No live_trades.csv at {path}\n"
                 "  On the VPS: sudo -u bot env RAILWAY_VOLUME_MOUNT_PATH=/opt/polymarket-bot/data \\\n"
                 "    venv/bin/python scripts/no_band_gate.py")
    out = []
    with path.open() as f:
        for r in csv.DictReader(f):
            if r.get("order_status") != "matched":
                continue
            size, px, pnl = _num(r.get("filled_size")), _num(r.get("filled_price")), _num(r.get("pnl_usd"))
            if not size or px is None or pnl is None or r.get("actual_outcome", "") == "":
                continue
            stake = size * px
            out.append({"dir": r.get("direction", ""), "px": px,
                        "pnl": pnl - taker_fee_usd(stake, px),
                        "t": r.get("signal_time", "")})
    return out


def _dropped(rows: list[dict], cut: float) -> list[dict]:
    return [r for r in rows if r["dir"] == "NO" and r["px"] >= cut]


def _boot_p(drop: list[dict]) -> float:
    """P(dropped-set PnL < 0) under day-clustered resampling."""
    by_day: dict[str, list[dict]] = defaultdict(list)
    for r in drop:
        by_day[r["t"][:10]].append(r)
    days = list(by_day.values())
    if not days:
        return 0.0
    rng = random.Random(BOOT_SEED)
    neg = 0
    for _ in range(BOOT_N):
        sample = [r for d in (rng.choice(days) for _ in days) for r in d]
        if sum(r["pnl"] for r in sample) < 0:
            neg += 1
    return neg / BOOT_N


def evaluate(rows: list[dict]) -> dict:
    """The gate's whole logic, pure, so tests can drive it. `rows` are already
    restricted to signal_time >= LOCK_DATE."""
    drop = _dropped(rows, NO_CUT)
    keep_pnl = sum(r["pnl"] for r in rows) - sum(r["pnl"] for r in drop)
    res = {"n": len(rows), "n_drop": len(drop), "answerable": False,
           "drop_pnl": sum(r["pnl"] for r in drop), "keep_pnl": keep_pnl,
           "context": {c: sum(r["pnl"] for r in _dropped(rows, c)) for c in CONTEXT_CUTS}}
    if len(rows) < MIN_N or len(drop) < MIN_DROP_N:
        return res
    res["answerable"] = True
    ordered = sorted(drop, key=lambda r: r["t"])
    half = len(ordered) // 2
    p1, p2 = sum(r["pnl"] for r in ordered[:half]), sum(r["pnl"] for r in ordered[half:])
    res.update(p_first=p1, p_second=p2, both_neg=(p1 < 0 and p2 < 0), boot_p=_boot_p(drop))
    res["proceed"] = res["drop_pnl"] < 0 and res["both_neg"] and res["boot_p"] >= BOOT_P_MIN
    return res


def main() -> None:
    print(f"NO-band gate — skip NO entries priced >= {NO_CUT:.2f}?\n")
    new = [r for r in load_fills() if r["t"] >= LOCK_DATE]
    r = evaluate(new)

    print(f"GATE (locked {LOCK_DATE}): need n>={MIN_N} resolved live fills AND "
          f">={MIN_DROP_N} NO fills at >={NO_CUT:.2f}. So far: n={r['n']}, dropped-set n={r['n_drop']}.\n")
    print(f"  dropped-set net PnL {r['drop_pnl']:+.2f}  (skipping them would change total by "
          f"{-r['drop_pnl']:+.2f}); kept-set net PnL {r['keep_pnl']:+.2f}")
    for c, p in r["context"].items():
        print(f"  context only: cut {c:.2f} -> dropped-set net PnL {p:+.2f}")
    print()

    if not r["answerable"]:
        print("VERDICT: not answerable yet. Nothing to turn on — live_trades.csv already "
              "records everything this needs. Re-run later; do NOT act on the numbers above.")
        return

    print(f"  first half {r['p_first']:+.2f}, second half {r['p_second']:+.2f}  "
          f"(both negative: {r['both_neg']})")
    print(f"  day-clustered bootstrap P(dropped set loses) = {r['boot_p']:.2f} "
          f"(need >= {BOOT_P_MIN:.2f})")
    if r["proceed"]:
        print(f"\nVERDICT: PROCEED — NO at >= {NO_CUT:.2f} lost money net of fees out of "
              "sample. Add an env-overridable live NO price ceiling, small and monitored.")
    else:
        why = []
        if r["drop_pnl"] >= 0:
            why.append("dropped set did not lose")
        if not r["both_neg"]:
            why.append("not negative in both halves")
        if r["boot_p"] < BOOT_P_MIN:
            why.append(f"bootstrap P {r['boot_p']:.2f} < {BOOT_P_MIN:.2f}")
        print(f"\nVERDICT: DO NOT cut ({', '.join(why)}). The in-sample result did not "
              "replicate; NO entry range stays as is.")


if __name__ == "__main__":
    main()
