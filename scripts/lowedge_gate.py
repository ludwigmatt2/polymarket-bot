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
  - Only rows with signal_time >= LOCK_DATE and entry_price >= LIVE_MIN_ENTRY_PRICE
    (what live could actually buy), resolved on verified truth
    (label_source onchain/station), count.
  - PnL is NET of the Polymarket taker fee (weather/fees.py; the CSV's pnl_usd is
    gross of it).
  - Bands by claimed edge_pp: B1 = [0.08, 0.11), B2 = [0.05, 0.08). B3 =
    [0.03, 0.05) is context only and never decides anything. Rows >= 0.11
    duplicate production and are ignored.
  - A band is "supported" iff n >= MIN_N_BAND (150), the track has been running
    >= MIN_WEEKS (4), its net ROI > 0, a day-clustered bootstrap (same-day rows
    are correlated) gives P(ROI > 0) >= BOOT_P_MIN (0.90), and the net ROI has
    the same sign in both halves of the window.
  - Outcomes, decided independently of any in-sample story:
      B1 and B2 supported -> candidate: MIN_NET_EV_PP 0.08 -> 0.02 (edge >= 5pp)
      only B1 supported   -> candidate: MIN_NET_EV_PP 0.08 -> 0.05 (edge >= 8pp)
      B1 not supported    -> keep Gate 4 as is.
    B2 supported without B1 is NOT a reason to lower anything (a hole in the
    middle is noise). A PROCEED is a recommendation to implement behind an
    env-overridable switch and shadow-confirm, never an automatic change.

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
from weather.fees import row_fee_usd  # noqa: E402
from weather.paths import DATA_DIR  # noqa: E402

TRACK_LOG = DATA_DIR / "logs" / "shadow" / "lowedge" / "paper_trades.csv"
LOCK_DATE = "2026-10-15T00:00:00+00:00"  # do not move this once set
BANDS = {"B1": (0.08, 0.11), "B2": (0.05, 0.08), "B3": (0.03, 0.05)}
DECIDING = ("B1", "B2")  # B3 is context only
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


def load_rows(path: Path = TRACK_LOG) -> list[dict]:
    """Resolved, verified-truth rows as {edge, stake, net, t}. net = pnl - fee."""
    if not path.exists():
        print(f"(no lowedge track log at {path} yet — the track starts logging after the "
              "roster deploy and needs SHADOW_TRACKS_ENABLED=1)\n")
        return []
    out = []
    with path.open() as f:
        for r in csv.DictReader(f):
            if r.get("actual_outcome") not in ("0", "1"):
                continue
            if r.get("label_source") not in ("onchain", "station"):
                continue
            edge, px = _num(r.get("edge_pp")), _num(r.get("entry_price"))
            stake, pnl = _num(r.get("size_usd")), _num(r.get("pnl_usd"))
            if None in (edge, px, stake, pnl) or not stake or px < LIVE_MIN_ENTRY_PRICE:
                continue
            out.append({"edge": edge, "stake": stake, "t": r.get("signal_time", ""),
                        "net": pnl - row_fee_usd(r, stake, px)})
    return out


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


def evaluate_band(rows: list[dict], lo: float, hi: float, weeks: float) -> dict:
    band = sorted((r for r in rows if lo <= r["edge"] < hi), key=lambda r: r["t"])
    res = {"n": len(band), "roi": _roi(band), "supported": False}
    if len(band) < MIN_N_BAND or weeks < MIN_WEEKS or not band:
        return res
    half = len(band) // 2
    r1, r2 = _roi(band[:half]), _roi(band[half:])
    res.update(boot_p=_boot_p_positive(band), roi_first=r1, roi_second=r2)
    res["supported"] = (res["roi"] > 0 and res["boot_p"] >= BOOT_P_MIN
                        and r1 is not None and r2 is not None and r1 > 0 and r2 > 0)
    return res


def decide(bands: dict[str, dict]) -> str:
    b1, b2 = bands["B1"]["supported"], bands["B2"]["supported"]
    if b1 and b2:
        return "LOWER_TO_0.02"
    if b1:
        return "LOWER_TO_0.05"
    return "KEEP"


def _weeks_running(rows: list[dict]) -> float:
    if not rows:
        return 0.0
    first = min(r["t"] for r in rows)
    try:
        d0 = datetime.fromisoformat(first)
    except ValueError:
        return 0.0
    return (datetime.now(timezone.utc) - d0).days / 7.0


def main() -> None:
    print("Low-edge gate — can Gate 4's 11pp floor be lowered?\n")
    rows = [r for r in load_rows() if r["t"] >= LOCK_DATE]
    weeks = _weeks_running(rows)
    print(f"GATE (locked {LOCK_DATE}): each deciding band needs n>={MIN_N_BAND} and the "
          f"track running >={MIN_WEEKS}w. So far: {len(rows)} resolved rows, {weeks:.1f}w.\n")
    results = {}
    for name, (lo, hi) in BANDS.items():
        r = evaluate_band(rows, lo, hi, weeks)
        results[name] = r
        roi = "n/a" if r["roi"] is None else f"{r['roi']*100:+.1f}%"
        extra = (f"  P(ROI>0)={r['boot_p']:.2f}  halves {r['roi_first']*100:+.0f}% / "
                 f"{r['roi_second']*100:+.0f}%") if "boot_p" in r else ""
        tag = "" if name in DECIDING else "  (context only)"
        print(f"  {name} edge [{lo:.2f},{hi:.2f}): n={r['n']:4d}  net ROI {roi}{extra}{tag}")
    answerable = all(results[b]["n"] >= MIN_N_BAND for b in DECIDING) and weeks >= MIN_WEEKS
    if not answerable:
        print("\nVERDICT: not answerable yet. The shadow track is collecting this "
              "unconditionally — no config change needed. Do NOT lower Gate 4 on anything "
              "above; there is no data below 11pp except this track.")
        return
    d = decide(results)
    if d == "KEEP":
        print("\nVERDICT: KEEP Gate 4 as is — the 8-11pp band was not shown to earn "
              "net of fee out of sample.")
    else:
        cand = "0.02 (edge >= 5pp)" if d == "LOWER_TO_0.02" else "0.05 (edge >= 8pp)"
        print(f"\nVERDICT: PROCEED — candidate MIN_NET_EV_PP 0.08 -> {cand}. Implement "
              "behind an env-overridable switch and shadow-confirm before relying on it.")


if __name__ == "__main__":
    main()
