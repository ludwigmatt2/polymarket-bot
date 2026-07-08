"""
Telegram transparency formatters — pure functions over trade CSV rows and
last_signals.json data. No bot state, no I/O beyond what callers pass in,
so everything here is unit-testable with fixture rows.

/why <trade_id>  — full reasoning for one trade (model vs market, gates)
/scanreport      — scan funnel: fetched → parsed → gates → actionable
/losses [n]      — resolved losers with a one-line cause each
"""

from __future__ import annotations

import json

from weather.config import (
    MAX_ENSEMBLE_SPREAD,
    MIN_COMPOSITE_CONFIDENCE,
    EDGE_SAFETY_MARGIN_PP,
)

# Telegram hard limit is 4096 chars; leave headroom for markup overhead.
MAX_MSG_CHARS = 3900

_METRIC_LABELS = {
    "temperature_2m_max": "high temp",
    "temperature_2m_min": "low temp",
    "precipitation_sum": "precipitation",
    "snowfall_sum": "snowfall",
}


def _truncate(text: str) -> str:
    if len(text) <= MAX_MSG_CHARS:
        return text
    return text[: MAX_MSG_CHARS - 15].rstrip() + "\n_…truncated_"


def _f(row: dict, key: str, default: float | None = None) -> float | None:
    """Float field from a CSV row; '' and missing both → default."""
    val = row.get(key, "")
    if val in ("", None, "None"):
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _condition_str(row: dict) -> str:
    """Human description of what the market asked, e.g. 'high temp > 30.0'."""
    metric = _METRIC_LABELS.get(row.get("metric", ""), row.get("metric", "?"))
    direction = row.get("weather_direction", "?")
    thr = row.get("threshold", "?")
    thr_high = row.get("threshold_high", "")
    if direction == "range" and thr_high not in ("", None, "None"):
        return f"{metric} between {thr} and {thr_high}"
    op = {"above": ">", "below": "<", "equal": "="}.get(direction, direction)
    return f"{metric} {op} {thr}"


# ── /why ──────────────────────────────────────────────────────────────────────

def fmt_why(row: dict) -> str:
    """Explain one trade: model probability chain, edge math, gate context."""
    title = row.get("market_title", "?")
    direction = row.get("direction", "?")
    entry = _f(row, "entry_price")
    model_p = _f(row, "model_p")
    raw_p = _f(row, "raw_p")
    edge = _f(row, "edge_pp", 0.0)
    spread = _f(row, "ensemble_spread")
    conf = _f(row, "confidence_score")
    size = _f(row, "size_usd", 0.0)

    # entry_price is the price of the side bought; the market's P(YES) is
    # entry for YES bets and 1-entry for NO bets.
    yes_price = entry if direction == "YES" else (1.0 - entry) if entry is not None else None

    lines = [f"*❓ Why this trade?*\n_{title[:80]}_\n"]
    lines.append(f"Bet: *{direction}* · ${size:.2f} · condition: {_condition_str(row)}")

    if model_p is not None and yes_price is not None:
        lines.append("\n*Model vs market* (P of YES)")
        if raw_p is not None:
            shift = model_p - raw_p
            lines.append(f"Ensemble raw:   {raw_p:.1%}")
            lines.append(f"After shrink:   {model_p:.1%}  ({shift:+.1%} toward market)")
        else:
            lines.append(f"Model:          {model_p:.1%}")
        lines.append(f"Market priced:  {yes_price:.1%}")
        lines.append(
            f"Edge:           *{edge:.1%}* (≥ {EDGE_SAFETY_MARGIN_PP:.0%} margin needed)"
        )

    # Per-model breakdown from the stored JSON
    breakdown_json = row.get("model_breakdown_json", "")
    if breakdown_json:
        try:
            breakdown = json.loads(breakdown_json)
        except (json.JSONDecodeError, TypeError):
            breakdown = {}
        if breakdown:
            lines.append("\n*Per-model P(YES)*")
            for mdl, p in sorted(breakdown.items(), key=lambda x: x[1]):
                lines.append(f"  {mdl}: {float(p):.1%}")

    lines.append("\n*Quality gates at entry*")
    if spread is not None:
        ok = "✅" if spread <= MAX_ENSEMBLE_SPREAD else "⚠️"
        lines.append(f"{ok} Model agreement: spread {spread:.3f} (max {MAX_ENSEMBLE_SPREAD})")
    if conf is not None:
        ok = "✅" if conf >= MIN_COMPOSITE_CONFIDENCE else "⚠️"
        lines.append(f"{ok} Confidence: {conf:.2f} (min {MIN_COMPOSITE_CONFIDENCE})")

    # Outcome (if resolved)
    outcome = row.get("actual_outcome", "")
    if outcome in ("0", "1"):
        pnl = _f(row, "pnl_usd", 0.0)
        won = pnl > 0
        res = "YES" if outcome == "1" else "NO"
        e = "✅" if won else "❌"
        lines.append(f"\n{e} *Resolved {res}* → ${pnl:+.2f}")
        brier = _f(row, "brier_score")
        if brier is not None:
            lines.append(f"Brier: {brier:.4f} (lower = better; 0.25 = coin flip)")

    return _truncate("\n".join(lines))


def find_trade(rows: list[dict], trade_id: str) -> dict | None:
    """Look up a trade by full or prefix trade_id."""
    for r in rows:
        if r.get("trade_id") == trade_id:
            return r
    matches = [r for r in rows if r.get("trade_id", "").startswith(trade_id)]
    return matches[0] if len(matches) == 1 else None


# ── /scanreport ───────────────────────────────────────────────────────────────

def fmt_scanreport(signals_data: dict) -> str:
    """Render the scan funnel persisted in last_signals.json."""
    funnel = signals_data.get("funnel")
    scanned_at = signals_data.get("scanned_at", "")[:16].replace("T", " ")
    if not funnel:
        return (
            "No funnel data yet — it's recorded from the next scan onward.\n"
            "Trigger one with /scan."
        )

    lines = ["*🔬 Scan Report*"]
    if scanned_at:
        lines.append(f"_Scanned: {scanned_at} UTC_\n")

    fetched = funnel.get("fetched", "?")
    parsed = funnel.get("parsed", "?")
    unparseable = funnel.get("unparseable", 0)
    tradeable = funnel.get("tradeable", "?")
    evaluated = funnel.get("evaluated", "?")
    actionable = funnel.get("actionable", "?")

    lines.append(f"Fetched:    {fetched} markets")
    lines.append(f"Parsed:     {parsed}  ({unparseable} unparseable)")
    lines.append(f"Tradeable:  {tradeable}  (liquidity/date filters)")
    lines.append(f"Evaluated:  {evaluated}  (model ran on each)")
    lines.append(f"*Actionable: {actionable}*  (passed all quality gates)")

    rejections = funnel.get("rejections", {})
    if rejections:
        lines.append("\n*Rejections by gate*")
        for gate, n in rejections.items():
            lines.append(f"  {gate}: {n}")

    top_rejected = funnel.get("top_rejected", [])
    if top_rejected:
        lines.append("\n*Near-misses* (highest edge among rejected)")
        for r in top_rejected[:5]:
            title = r.get("title", "?")
            if " - " in title:
                title = title.split(" - ", 1)[1]
            lines.append(f"  {r.get('edge_pp', 0):.0%} — _{title[:45]}_")
            lines.append(f"      ✗ {r.get('reason', '?')[:60]}")

    return _truncate("\n".join(lines))


# ── /losses ───────────────────────────────────────────────────────────────────

def fmt_losses(rows: list[dict], n: int = 10) -> str:
    """Resolved losing trades, biggest loss first, each with the cause."""
    losers = [
        r for r in rows
        if r.get("resolved_at") and (_f(r, "pnl_usd", 0.0) or 0.0) < 0
    ]
    if not losers:
        return "🎉 No losing trades on record."

    losers.sort(key=lambda r: _f(r, "pnl_usd", 0.0))
    shown = losers[:n]
    total_lost = sum(_f(r, "pnl_usd", 0.0) for r in losers)

    lines = [
        f"*📉 Losses* — {len(losers)} losing trades · ${total_lost:,.2f} total\n"
    ]
    for r in shown:
        pnl = _f(r, "pnl_usd", 0.0)
        direction = r.get("direction", "?")
        model_p = _f(r, "model_p")
        title = r.get("market_title", "?")
        if " - " in title:
            title = title.split(" - ", 1)[1]

        lines.append(f"❌ *${pnl:+.2f}* · {direction} · _{title[:48]}_")
        # The cause line: what the model believed vs what happened.
        outcome = "YES" if r.get("actual_outcome") == "1" else "NO"
        needed = "NO" if direction == "NO" else "YES"
        cause = f"   needed {_condition_str(r)} → {needed}, resolved {outcome}"
        if model_p is not None:
            cause += f" (model said {model_p:.0%} YES)"
        lines.append(cause)
        tid = r.get("trade_id", "")
        if tid:
            lines.append(f"   `/why {tid}`")

    if len(losers) > n:
        lines.append(f"\n_...and {len(losers) - n} more (`/losses {min(len(losers), 30)}`)_")

    return _truncate("\n".join(lines))
