"""Pre-registered gate: should LIVE_MIN_ENTRY_PRICE (the $0.40 floor) be lowered?

Context. weather/config.py:LIVE_MIN_ENTRY_PRICE=0.40 blocks live from trading any
market priced under $0.40. It was set from an IN-SAMPLE finding (memory:
longshot_price_leak — sub-$0.40 edge -0.046, and a real-fills era-replay: 18 fills
blocked, 1 win, -$38.79). The out-of-sample check (memory: edge_decay_sep2026)
REVERSED that finding to +0.116 in the same band. A band whose sign flips across
measurement windows is not evidence either way — it is exactly the pattern the
project's own operating rule exists to catch: "no model decision under ~250
resolved trades ... every result reported segmented by price band. Any 2-week
window can show anything." (docs/PHASE3_STATION_FORECAST_PLAN.md, T1's rule,
generalized from edge_decay_sep2026.md.)

Why this script, given the data already exists. data/logs/paper_trades.csv logs
EVERY actionable signal unconditionally (memory: paper_track_decoupled_from_live)
— sub-$0.40 included, live's floor never touches it. So nothing new needs to be
"turned on" to collect this; what was missing is a PRE-REGISTERED verdict gate
that refuses to answer before there is enough NEW data, the same discipline
mosmix_backcheck.py applies to the Phase 3 T1 question. This script is that gate.

THE GATE (locked 2026-09-30, before this script has scored a single day of new
data — do not loosen these numbers after seeing a result; that is the exact
failure mode this script exists to prevent):
  - Only rows with signal_time >= LOCK_DATE count toward the verdict. The prior
    record (shown below as CONTEXT ONLY) already flipped sign three times across
    three different windows and must not be cherry-picked as the answer.
  - n >= MIN_N (250) resolved sub-$0.40 signals AND elapsed time >= MIN_WEEKS (6)
    since LOCK_DATE — BOTH required (edge_decay_sep2026.md: "n>=250 OR 6 weeks,
    whichever is LATER" — i.e. do not accept either alone).
  - edge = realized_win_rate - market_price, correlation-corrected: same-day
    signals on the same city/metric are not independent draws, so raw n
    overstates precision. Deflate by CORR_FACTOR=1.61 (the factor
    edge_decay_sep2026.md's own read used) before computing z.
  - PROCEED only if |z| computed on the corrected n clears Z_THRESHOLD (2.0) AND
    the edge's sign is the same in both halves of the qualifying window (this
    project's own segment-and-recheck habit, not a single pooled number that can
    hide a mid-window flip).

Run: venv/bin/python scripts/price_floor_gate.py
"""
from __future__ import annotations

import csv
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from weather.config import GATE_ERA_START  # noqa: E402
from weather.paths import DATA_DIR  # noqa: E402

PAPER_LOG = DATA_DIR / "logs" / "paper_trades.csv"
BAND_HIGH = 0.40  # the current floor — this script only ever looks BELOW it
LOCK_DATE = "2026-09-30T00:00:00+00:00"  # do not move this once set
MIN_N = 250
MIN_WEEKS = 6
CORR_FACTOR = 1.61
Z_THRESHOLD = 2.0


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _won(row: dict) -> bool:
    yes = row["actual_outcome"] == "1"
    return yes if row.get("direction") == "YES" else not yes


def _load_band() -> list[dict]:
    """Every resolved, non-longshot, sub-$0.40 row — the file is read once here;
    callers slice this list by signal_time rather than each re-reading it."""
    if not PAPER_LOG.exists():
        sys.exit(f"No paper_trades.csv at {PAPER_LOG}\n"
                  "  On the VPS: sudo -u bot env RAILWAY_VOLUME_MOUNT_PATH=/opt/polymarket-bot/data \\\n"
                  "    venv/bin/python scripts/price_floor_gate.py")
    with PAPER_LOG.open() as f:
        rows = list(csv.DictReader(f))
    out = []
    for r in rows:
        if r.get("actual_outcome") not in ("0", "1"):
            continue
        if (r.get("scan_source") or "") == "longshot":
            continue
        ep = _num(r.get("entry_price"))
        if ep is None or ep >= BAND_HIGH:
            continue
        out.append(r)
    return out


def _since(rows: list[dict], since: str) -> list[dict]:
    return [r for r in rows if str(r.get("signal_time", "")) >= since]


def _edge_and_z(rows: list[dict]) -> dict | None:
    """realized win rate vs market price, correlation-corrected z. Matches the
    exact method behind edge_decay_sep2026.md's own numbers (verified by
    reproducing its Aug6-30 row: n=167, edge=+0.093 -> z=+1.96 with this
    formula)."""
    n = len(rows)
    if n == 0:
        return None
    realized = sum(_won(r) for r in rows) / n
    market = sum(_num(r["entry_price"]) for r in rows) / n
    edge = realized - market
    eff_n = n / CORR_FACTOR
    var = realized * (1 - realized) / eff_n if 0 < realized < 1 else None
    z = edge / (var ** 0.5) if var else None
    return {"n": n, "realized": realized, "market": market, "edge": edge, "z": z}


def _weeks_since(iso: str) -> float:
    dt = datetime.fromisoformat(iso)
    return (datetime.now(timezone.utc) - dt).days / 7.0


def main() -> None:
    print(f"Price-floor gate — is the ${BAND_HIGH:.2f} floor (LIVE_MIN_ENTRY_PRICE) "
          f"worth lowering?\n")

    all_rows = _load_band()

    # ── context: the pre-lock record — NEVER the verdict ────────────────────
    pre_lock = [r for r in _since(all_rows, GATE_ERA_START) if str(r.get("signal_time", "")) < LOCK_DATE]
    cs = _edge_and_z(pre_lock)
    print("CONTEXT ONLY (pre-lock record, already known to have flipped sign across "
          "windows — see edge_decay_sep2026.md / longshot_price_leak.md):")
    if cs:
        z_str = f"{cs['z']:+.2f}" if cs["z"] is not None else "n/a"
        print(f"  {GATE_ERA_START} .. {LOCK_DATE}: n={cs['n']:4}  realized={cs['realized']:.3f}  "
              f"market={cs['market']:.3f}  edge={cs['edge']:+.3f}  z(corr)={z_str}")
    else:
        print("  no rows.")
    print("  This number is NOT the gate. It is shown only so a reader doesn't have to\n"
          "  take the docstring's word for why a pooled historical read isn't trusted here.\n")

    # ── the actual gate: only NEW data since LOCK_DATE counts ───────────────
    rows = _since(all_rows, LOCK_DATE)
    weeks = _weeks_since(LOCK_DATE)
    s = _edge_and_z(rows)
    n = s["n"] if s else 0

    print(f"GATE (locked {LOCK_DATE}): need n>={MIN_N} AND elapsed>={MIN_WEEKS}w. "
          f"So far: n={n}, elapsed={weeks:.1f}w.\n")

    if n < MIN_N or weeks < MIN_WEEKS:
        print("VERDICT: not answerable yet. Keep the bot running (paper logs this band "
              "unconditionally already — no config change needed to keep collecting) and "
              "re-run this script later. Do NOT act on the context table above.")
        return

    # Only reached once both thresholds pass — split the qualifying window in half
    # and require the sign to agree, per this project's segment-and-recheck habit.
    rows_sorted = sorted(rows, key=lambda r: r["signal_time"])
    mid = len(rows_sorted) // 2
    first_half, second_half = _edge_and_z(rows_sorted[:mid]), _edge_and_z(rows_sorted[mid:])
    same_sign = (first_half and second_half and
                 (first_half["edge"] > 0) == (second_half["edge"] > 0))

    z_str = f"{s['z']:+.2f}" if s["z"] is not None else "n/a"
    print(f"Full qualifying window: n={s['n']}  realized={s['realized']:.3f}  "
          f"market={s['market']:.3f}  edge={s['edge']:+.3f}  z(corr)={z_str}")
    if first_half and second_half:
        print(f"  first half:  n={first_half['n']:4}  edge={first_half['edge']:+.3f}")
        print(f"  second half: n={second_half['n']:4}  edge={second_half['edge']:+.3f}")

    if s["z"] is not None and s["z"] >= Z_THRESHOLD and same_sign:
        print(f"\nVERDICT: PROCEED — corrected z={z_str} clears +{Z_THRESHOLD}, sign stable "
              "across both halves. Worth a deliberate, small, monitored change to "
              "LIVE_MIN_ENTRY_PRICE, not a blanket removal.")
    else:
        why = []
        if s["z"] is None or s["z"] < Z_THRESHOLD:
            why.append(f"z did not clear +{Z_THRESHOLD}")
        if not same_sign:
            why.append("sign was not stable across both halves")
        print(f"\nVERDICT: DO NOT lower the floor ({', '.join(why)}). The floor stays as "
              "variance reduction, not proven edge — same conclusion as before, now on "
              "fresh out-of-sample data instead of the flip-flopping historical record.")


if __name__ == "__main__":
    main()
