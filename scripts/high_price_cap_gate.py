"""Pre-registered gate: would a size cap on entry >= 0.60 have paid for itself?

Context (2026-10-15 backtest, in-sample, n=38 live fills at filled_price>=0.60).
The band won 68% against a ~67% break-even — no edge — and lost money ONLY
because of stake size: 14 trades above 1.5x the 0.40-0.60 median stake went
50% win / -$26.24, the 24 smaller ones 79% win / +$17.65. Kelly sizes up
exactly where model_p is most extreme (mp 0.08-0.18 at fp 0.65-0.78), and those
are the largest losers. A cap at 1.0x the mid-band median stake would have
turned the band from -$8.60 to +$2.96. Paper cannot confirm or refute this: it
sizes flat (~$20), so the overbetting never exists there; and paper's own
>=0.60 band is profitable (n=64, 81% win), which is ~1.7 sigma from live's 68%.

That is one in-sample read with a cap level chosen after seeing the data. Per
this project's operating rule (no model/sizing decision under ~250 resolved
trades; every result segmented by price band) it is NOT acted on. This script
is the pre-registered verdict gate, the same shape as price_floor_gate.py.

Why a script and not a ModelSpec shadow track: shadow tracks re-score the
MODEL ("execution/sizing is never part of a shadow track", weather/shadow.py)
and PaperTrader sizes flat, so a sizing counterfactual can't live there. The
cap is a deterministic function of what live_trades.csv already records
(filled_size, filled_price, pnl_usd), so nothing needs to be logged or
deployed to collect the evidence — this only has to refuse to answer early.

THE GATE (locked 2026-10-15 before scoring any new data — do not loosen these
numbers after seeing a result):
  - Only live fills with signal_time >= LOCK_DATE count. The 38 in-sample trades
    that motivated the cap are CONTEXT ONLY.
  - PRIMARY variant, fixed: cap stake at CAP_MULT (1.0) x the median stake of
    the 0.40-0.60 band, for trades with filled_price >= BAND_LOW (0.60). Other
    cap levels are printed as context and never decide anything.
  - Answerable only when n >= MIN_N (250) resolved live fills since LOCK_DATE
    AND >= MIN_BAND_N (40) of them are in the >=0.60 band. (MIN_BAND_N is an
    addition to the "n>=250" rule: at ~25% band share, 250 total fills is ~60
    band trades, and a verdict on 15 would be noise.)
  - PROCEED only if cap-minus-baseline band PnL > 0 AND a day-clustered
    bootstrap (same-day fills are correlated) says the cap beats baseline in
    >= BOOT_P_MIN (80%) of resamples AND the delta has the same sign in both
    halves of the window.
  - PnL under a cap is pnl * min(1, cap/stake): linear, no bankroll feedback.
  - AMENDED 2026-10-15 (same day, before any post-lock fill had resolved,
    n=0): all PnL is NET of the Polymarket taker fee (weather/fees.py,
    0.05*p*(1-p) per share, measured on-chain). live_trades.csv's pnl_usd is
    gross of it; with a ~2.4% fee the band's "break-even" win rate moves up and
    a gross-only gate would flatter every variant. The in-sample numbers quoted
    above are gross and are context only.

Run: venv/bin/python scripts/high_price_cap_gate.py
"""
from __future__ import annotations

import csv
import random
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from weather.fees import taker_fee_usd  # noqa: E402
from weather.paths import DATA_DIR  # noqa: E402

LIVE_LOG = DATA_DIR / "logs" / "live_trades.csv"
LOCK_DATE = "2026-10-15T00:00:00+00:00"  # do not move this once set
BAND_LOW = 0.60       # cap applies to filled_price >= this
MID_LOW = 0.40        # reference band for the cap level: [MID_LOW, BAND_LOW)
CAP_MULT = 1.0        # PRIMARY cap = CAP_MULT x median mid-band stake
CONTEXT_MULTS = (1.5, 2.0)
MIN_N = 250
MIN_BAND_N = 40
BOOT_P_MIN = 0.80
BOOT_N = 5000
BOOT_SEED = 1


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def load_fills(path: Path = LIVE_LOG) -> list[dict]:
    """Resolved, actually-filled live rows as {px, stake, pnl, t}. stake is the
    USD cost (filled_size * filled_price), the quantity a cap would bound; pnl
    is NET of the taker fee."""
    if not path.exists():
        sys.exit(f"No live_trades.csv at {path}\n"
                 "  On the VPS: sudo -u bot env RAILWAY_VOLUME_MOUNT_PATH=/opt/polymarket-bot/data \\\n"
                 "    venv/bin/python scripts/high_price_cap_gate.py")
    out = []
    with path.open() as f:
        for r in csv.DictReader(f):
            if r.get("order_status") != "matched":
                continue
            size, px, pnl = _num(r.get("filled_size")), _num(r.get("filled_price")), _num(r.get("pnl_usd"))
            if not size or px is None or pnl is None or r.get("actual_outcome", "") == "":
                continue
            stake = size * px
            out.append({"px": px, "stake": stake, "pnl": pnl - taker_fee_usd(stake, px),
                        "t": r.get("signal_time", "")})
    return out


def _capped(row: dict, cap: float) -> float:
    return row["pnl"] * min(1.0, cap / row["stake"]) if row["stake"] > 0 else row["pnl"]


def _delta(rows: list[dict], cap: float) -> float:
    return sum(_capped(r, cap) - r["pnl"] for r in rows)


def _boot_p(band: list[dict], cap: float) -> float:
    """P(capped band PnL > baseline) under day-clustered resampling."""
    by_day: dict[str, list[dict]] = defaultdict(list)
    for r in band:
        by_day[r["t"][:10]].append(r)
    days = list(by_day.values())
    if not days:
        return 0.0
    rng = random.Random(BOOT_SEED)
    wins = 0
    for _ in range(BOOT_N):
        sample = [r for d in (rng.choice(days) for _ in days) for r in d]
        if _delta(sample, cap) > 0:
            wins += 1
    return wins / BOOT_N


def evaluate(rows: list[dict]) -> dict:
    """The gate's whole logic, pure, so tests can drive it. `rows` are
    already restricted to signal_time >= LOCK_DATE."""
    band = [r for r in rows if r["px"] >= BAND_LOW]
    mid = [r for r in rows if MID_LOW <= r["px"] < BAND_LOW]
    res = {"n": len(rows), "n_band": len(band), "n_mid": len(mid), "answerable": False}
    if not mid:
        return res
    ref = statistics.median(r["stake"] for r in mid)
    cap = CAP_MULT * ref
    res.update(ref=ref, cap=cap,
               base=sum(r["pnl"] for r in band),
               delta=_delta(band, cap),
               context={m: _delta(band, m * ref) for m in CONTEXT_MULTS})
    if len(rows) < MIN_N or len(band) < MIN_BAND_N:
        return res
    res["answerable"] = True
    ordered = sorted(band, key=lambda r: r["t"])
    half = len(ordered) // 2
    d1, d2 = _delta(ordered[:half], cap), _delta(ordered[half:], cap)
    res.update(d_first=d1, d_second=d2, same_sign=(d1 > 0) == (d2 > 0),
               boot_p=_boot_p(band, cap))
    res["proceed"] = res["delta"] > 0 and res["same_sign"] and res["boot_p"] >= BOOT_P_MIN
    return res


def main() -> None:
    print("High-price size-cap gate — cap stakes at entry >= "
          f"{BAND_LOW:.2f}?\n")
    fills = load_fills()
    new = [r for r in fills if r["t"] >= LOCK_DATE]
    r = evaluate(new)

    print(f"GATE (locked {LOCK_DATE}): need n>={MIN_N} resolved live fills AND "
          f">={MIN_BAND_N} in the >={BAND_LOW:.2f} band. "
          f"So far: n={r['n']}, band n={r['n_band']}.\n")
    if "cap" in r:
        print(f"Primary cap = {CAP_MULT:.1f} x median mid-band stake "
              f"(${r['ref']:.2f}) = ${r['cap']:.2f}")
        print(f"  band PnL baseline {r['base']:+.2f}  ->  capped {r['base'] + r['delta']:+.2f}  "
              f"(delta {r['delta']:+.2f})")
        for m, d in r["context"].items():
            print(f"  context only: cap {m:.1f}x -> delta {d:+.2f}")
        print()

    if not r["answerable"]:
        print("VERDICT: not answerable yet. Nothing to turn on — live_trades.csv already "
              "records everything this needs. Re-run later; do NOT act on the delta above.")
        return

    print(f"  first half delta {r['d_first']:+.2f}, second half {r['d_second']:+.2f}  "
          f"(same sign: {r['same_sign']})")
    print(f"  day-clustered bootstrap P(cap beats baseline) = {r['boot_p']:.2f} "
          f"(need >= {BOOT_P_MIN:.2f})")
    if r["proceed"]:
        print("\nVERDICT: PROCEED — a stake cap on the high-price band paid for itself "
              "out of sample. Implement as an env-overridable live kill switch, "
              "small and monitored.")
    else:
        why = []
        if r["delta"] <= 0:
            why.append("delta <= 0")
        if not r["same_sign"]:
            why.append("sign not stable across halves")
        if r["boot_p"] < BOOT_P_MIN:
            why.append(f"bootstrap P {r['boot_p']:.2f} < {BOOT_P_MIN:.2f}")
        print(f"\nVERDICT: DO NOT cap ({', '.join(why)}). The in-sample result did not "
              "replicate; sizing stays as is.")


if __name__ == "__main__":
    main()
