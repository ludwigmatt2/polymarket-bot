"""Real (on-chain) deposit ledger — separate from the paper wallet ledger.

The paper ledger (`wallet.json`, in telegram_bot) tracks *simulated* capital typed
via `/deposit` in paper mode. This ledger tracks *real* money that arrived on-chain,
so live ROI can be computed from actual capital invested — never a typed number.

Deposits are detected from **USDC.e**, because that is what a funding transfer
arrives as, before it is wrapped to pUSD at go-live. USDC.e only ever moves up (a
new deposit) or down (a wrap) — trading P&L accrues in pUSD — so an *increase* in
USDC.e is an unambiguous deposit signal, uncontaminated by winnings.

One JSON file per user; callers pass the path (admin/user routing lives in the bot).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from ._io import atomic_write_text


def read_ledger(path: Path) -> dict:
    if not path.exists():
        return {"transactions": [], "last_usdce": 0.0}
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {"transactions": [], "last_usdce": 0.0}
    data.setdefault("transactions", [])
    data.setdefault("last_usdce", 0.0)
    return data


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(data, indent=2), mode=0o600)


def record_transaction(path: Path, tx_type: str, amount: float, note: str = "") -> None:
    """Append a deposit/withdraw entry explicitly (e.g. a real /withdraw)."""
    data = read_ledger(path)
    data["transactions"].append({
        "type": tx_type,
        "amount": round(float(amount), 6),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "note": note,
    })
    _write(path, data)


def reconcile_deposit(path: Path, usdce_now: float, *, min_delta: float = 0.01) -> float:
    """Detect a new deposit from the current on-chain USDC.e balance.

    Records the *increase* since the last observation as a deposit and returns it
    (0.0 if none). A decrease (a wrap to pUSD) is not a deposit — we just lower the
    watermark so the next genuine deposit is measured from there. Idempotent per
    balance level: re-checking the same balance records nothing.
    """
    data = read_ledger(path)
    last = float(data.get("last_usdce", 0.0))
    detected = 0.0
    delta = round(usdce_now - last, 6)
    if delta >= min_delta:
        detected = delta
        data["transactions"].append({
            "type": "deposit",
            "amount": detected,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "note": "on-chain USDC.e deposit detected",
        })
    if usdce_now != last:
        data["last_usdce"] = round(usdce_now, 6)
        _write(path, data)
    return detected


def totals(path: Path) -> dict:
    """Gross deposited / withdrawn / net, for the wallet view."""
    txns = read_ledger(path).get("transactions", [])
    dep = sum(t["amount"] for t in txns if t.get("type") == "deposit")
    wd = sum(t["amount"] for t in txns if t.get("type") == "withdraw")
    return {"deposited": round(dep, 6), "withdrawn": round(wd, 6), "net": round(dep - wd, 6)}


def net_deposited(path: Path) -> float:
    """Net real capital in = deposits − withdrawals. The basis for live ROI."""
    return totals(path)["net"]
