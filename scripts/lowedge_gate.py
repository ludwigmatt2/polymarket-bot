"""Pre-registered gate: can Gate 4's floor be lowered below 11pp claimed edge?

Context (2026-10-15). Gate 4 admits a signal only if claimed edge - 3pp haircut
>= MIN_NET_EV_PP (8pp), i.e. edge >= 11pp. Every resolved trade we own sits at
or above that floor (the lowest logged edge_pp is 0.11), so there is NO data on
what the 3-11pp band earns. Replaying a fee-aware floor over the existing trades
showed the marginal 11-12.5pp band was net-profitable (paper n=234 +9.5% ROI),
which argues for looking, not for dropping the gate blind: the model's Brier
(0.2229) does not beat the market's (0.2224), claimed edge averages ~13pp but
realized ~+1.7pp live / +5.9pp paper gross, and the taker fee is ~1.2pp/share.
Break-even claimed edge is 1.2 / k, k = realized/claimed in [~0.13, ~0.45], i.e.
anywhere from ~2.7pp to ~9pp. The data can't distinguish those from here.

The probe: shadow track "lowedge" (weather/shadow.py) runs production's exact
recipe with Gate 4 relaxed to net_ev >= 0.0 (claimed edge >= 3pp), every other
gate unchanged, and logs to data/logs/shadow/lowedge/paper_trades.csv. Nothing
live changes. This script is the verdict.

THE GATE (locked 2026-10-15 before the track logged a single row — do not
loosen these numbers after seeing a result):
  - Only rows with signal_time >= LOCK_DATE, resolved on verified truth
    (label_source onchain/station), whose EXECUTABLE price >= LIVE_MIN_ENTRY_PRICE
    (what live could actually buy), count.
  - Rows are scored at the traded side's ASK (weather.fees.executable_cost =
    p_win - edge_pp), NOT at the paper row's entry_price: PaperTrader logs the
    MID, while edge_pp (the band variable) is measured against the ask, so
    scoring at the mid credits a ~0.7c/share better fill than live gets.
    Rows lacking model_p/edge_pp fall back to entry_price.
  - PnL is NET of the Polymarket taker fee (weather/fees.py; the CSV's pnl_usd is
    gross of it).
  - Bands by claimed edge_pp: B1 = [0.08, 0.11), B2 = [0.05, 0.08). B3 =
    [0.03, 0.05) is context only and never decides anything. Rows >= 0.11
    duplicate production and are ignored.
  - A band is "supported" iff n >= MIN_N_BAND (150), the track has been running
    >= MIN_WEEKS (4, clocked from the track's FIRST logged row, resolved or not),
    its net ROI > 0, a day-clustered bootstrap (same-day rows are correlated)
    gives P(ROI > 0) >= BOOT_P_MIN (0.90), and the net ROI has the same sign in
    both halves, split on a calendar-day boundary (never inside one scan's
    identically-timestamped cluster).
  - Outcomes, decided independently of any in-sample story:
      B1 not supported            -> keep Gate 4 as is. Decidable as soon as B1
                                     alone has n>=150 and weeks>=4 — B2 is
                                     irrelevant when B1 fails.
      B1 supported, B2 supported  -> candidate: MIN_NET_EV_PP 0.08 -> 0.02
      B1 supported, B2 not        -> candidate: MIN_NET_EV_PP 0.08 -> 0.05
      B1 supported, B2 n<150      -> candidate 0.05 stands; 0.02 not decidable yet
    B2 supported without B1 is NOT a reason to lower anything (a hole in the
    middle is noise). A PROCEED is a recommendation to implement behind an
    env-overridable switch and shadow-confirm, never an automatic change.

Known limitation, stated up front: like every shadow track, lowedge keeps an
ISOLATED calibrator that learns only from its own (lower-edge) trades after the
warm-start, so its model_p can drift from production's. The band ROI therefore
reflects "this track's model with a lower floor", approximately — not a perfectly
clean single-variable counterfactual.

Run: venv/bin/python scripts/lowedge_gate.py
"""
from __future__ import annotations

import csv
import random
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from weather.config import LIVE_MIN_ENTRY_PRICE  # noqa: E402
from weather.fees import executable_cost, row_fee_usd  # noqa: E402
from weather.paths import DATA_DIR  # noqa: E402

TRACK_LOG = DATA_DIR / "logs" / "shadow" / "lowedge" / "paper_trades.csv"
LOCK_DATE = "2026-10-15T00:00:00+00:00"  # do not move this once set
BANDS = {"B1": (0.08, 0.11), "B2": (0.05, 0.08), "B3": (0.03, 0.05)}
MIN_N_BAND = 150
MIN_WEEKS = 4
BOOT_P_MIN = 0.90
BOOT_N = 5000
BOOT_SEED = 1


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _parse_time(s: str) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _read(path: Path) -> list[dict]:
    if not path.exists():
        print(f"(no lowedge track log at {path} yet — the track starts logging after the "
              "roster deploy and needs SHADOW_TRACKS_ENABLED=1)\n")
        return []
    with path.open() as f:
        return list(csv.DictReader(f))


def score_row(r: dict) -> dict | None:
    """One resolved paper row -> {edge, stake, net, t} scored at the executable
    ask, net of fee. None if it doesn't qualify."""
    if r.get("actual_outcome") not in ("0", "1"):
        return None
    if r.get("label_source") not in ("onchain", "station"):
        return None
    edge, entry = _num(r.get("edge_pp")), _num(r.get("entry_price"))
    stake, pnl = _num(r.get("size_usd")), _num(r.get("pnl_usd"))
    if None in (edge, entry, stake, pnl) or not stake:
        return None
    cost = executable_cost(r) or entry
    if cost < LIVE_MIN_ENTRY_PRICE or cost >= 1.0:
        return None
    won = pnl > 0  # at the mid entry a win always returns > 0
    gross = stake * (1.0 / cost - 1.0) if won else -stake
    # A fee_usd recorded at resolution was computed at the MID entry; at the ask the
    # fee is slightly different, so recompute rather than reuse it.
    fee = row_fee_usd({}, stake, cost)
    return {"edge": edge, "stake": stake, "net": gross - fee, "t": r.get("signal_time", "")}


def load_rows(path: Path = TRACK_LOG) -> list[dict]:
    return [s for s in (score_row(r) for r in _read(path)) if s]


def track_weeks(path: Path = TRACK_LOG) -> float:
    """Weeks since the track's first logged row (resolved or not) at/after LOCK_DATE.
    Unparseable timestamps are reported, never silently turned into 0 weeks."""
    times = []
    bad = 0
    for r in _read(path):
        t = r.get("signal_time", "")
        if t < LOCK_DATE:
            continue
        d = _parse_time(t)
        if d is None:
            bad += 1
        else:
            times.append(d)
    if bad:
        print(f"WARNING: {bad} row(s) with unparseable signal_time were ignored for the "
              "track-age clock.\n")
    if not times:
        return 0.0
    return (datetime.now(timezone.utc) - min(times)).total_seconds() / (7 * 86400.0)


def _roi(rows: list[dict]) -> float | None:
    c = sum(r["stake"] for r in rows)
    return sum(r["net"] for r in rows) / c if c else None


def _boot_p_positive(rows: list[dict]) -> float:
    by_day: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_day[r["t"][:10]].append(r)
    days = list(by_day.values())
    rng = random.Random(BOOT_SEED)
    ok = 0
    for _ in range(BOOT_N):
        roi = _roi([r for d in (rng.choice(days) for _ in days) for r in d])
        ok += roi is not None and roi > 0
    return ok / BOOT_N


def _halves_by_day(rows: list[dict]) -> tuple[list[dict], list[dict]] | None:
    days = sorted({r["t"][:10] for r in rows})
    if len(days) < 2:
        return None
    cut = days[len(days) // 2]  # first half = days strictly before the median day
    return ([r for r in rows if r["t"][:10] < cut], [r for r in rows if r["t"][:10] >= cut])


def evaluate_band(rows: list[dict], lo: float, hi: float, weeks: float) -> dict:
    band = [r for r in rows if lo <= r["edge"] < hi]
    res = {"n": len(band), "roi": _roi(band), "supported": False,
           "decidable": len(band) >= MIN_N_BAND and weeks >= MIN_WEEKS}
    if not res["decidable"]:
        return res
    halves = _halves_by_day(band)
    r1 = _roi(halves[0]) if halves else None
    r2 = _roi(halves[1]) if halves else None
    res.update(boot_p=_boot_p_positive(band), roi_first=r1, roi_second=r2)
    res["supported"] = (res["roi"] is not None and res["roi"] > 0
                        and res["boot_p"] >= BOOT_P_MIN
                        and r1 is not None and r2 is not None and r1 > 0 and r2 > 0)
    return res


def decide(bands: dict[str, dict]) -> str | None:
    """None = not decidable yet. B1 alone decides KEEP; B2 only refines a
    supported B1 into 0.05 vs 0.02."""
    b1, b2 = bands["B1"], bands["B2"]
    if not b1["decidable"]:
        return None
    if not b1["supported"]:
        return "KEEP"
    if b2["supported"]:
        return "LOWER_TO_0.02"
    if b2["decidable"]:
        return "LOWER_TO_0.05"
    return "LOWER_TO_0.05_B2_PENDING"


def main() -> None:
    print("Low-edge gate — can Gate 4's 11pp floor be lowered?\n")
    rows = [r for r in load_rows() if r["t"] >= LOCK_DATE]
    weeks = track_weeks()
    print(f"GATE (locked {LOCK_DATE}): B1 needs n>={MIN_N_BAND} and the track running "
          f">={MIN_WEEKS}w (B2 only refines a supported B1). "
          f"So far: {len(rows)} resolved rows, {weeks:.1f}w.\n")
    results = {}
    for name, (lo, hi) in BANDS.items():
        r = evaluate_band(rows, lo, hi, weeks)
        results[name] = r
        roi = "n/a" if r["roi"] is None else f"{r['roi']*100:+.1f}%"
        extra = ""
        if "boot_p" in r:
            f = lambda x: "n/a" if x is None else f"{x*100:+.0f}%"  # noqa: E731
            extra = (f"  P(ROI>0)={r['boot_p']:.2f}  halves {f(r['roi_first'])} / "
                     f"{f(r['roi_second'])}")
        tag = "  (context only)" if name == "B3" else ""
        print(f"  {name} edge [{lo:.2f},{hi:.2f}): n={r['n']:4d}  net ROI {roi}{extra}{tag}")
    d = decide(results)
    if d is None:
        print("\nVERDICT: not answerable yet. The shadow track is collecting this "
              "unconditionally — no config change needed. Do NOT lower Gate 4 on anything "
              "above; there is no data below 11pp except this track.")
    elif d == "KEEP":
        print("\nVERDICT: KEEP Gate 4 as is — the 8-11pp band was not shown to earn "
              "net of fee at the ask out of sample.")
    else:
        cand = {"LOWER_TO_0.02": "0.02 (edge >= 5pp)",
                "LOWER_TO_0.05": "0.05 (edge >= 8pp)",
                "LOWER_TO_0.05_B2_PENDING": "0.05 (edge >= 8pp); B2 not yet decidable, "
                                            "0.02 may follow"}[d]
        print(f"\nVERDICT: PROCEED — candidate MIN_NET_EV_PP 0.08 -> {cand}. Implement "
              "behind an env-overridable switch and shadow-confirm before relying on it.")


if __name__ == "__main__":
    main()
