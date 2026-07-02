"""
Append-only audit log (SECURITY_PLAN Phase F).

Records privileged / money-moving / permission events as one JSON object per line
in data/logs/audit.jsonl, so every sensitive action is reconstructable after the
fact. Writes are best-effort: an audit failure must never break the action it logs.

Example line:
  {"ts":"2026-07-02T09:00:00+00:00","action":"withdrawal","actor":123,"amount":5.0,"to":"0x…"}
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from .paths import DATA_DIR

AUDIT_LOG = DATA_DIR / "logs" / "audit.jsonl"


def audit_log(action: str, actor: int | None = None, **details) -> None:
    """Append one audit record. Never raises."""
    rec: dict = {"ts": datetime.now(timezone.utc).isoformat(), "action": action}
    if actor is not None:
        rec["actor"] = actor
    rec.update(details)
    try:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_LOG.open("a") as f:
            f.write(json.dumps(rec, separators=(",", ":"), default=str) + "\n")
    except OSError:
        pass  # auditing must not break the audited action


def read_audit(n: int = 50) -> list[dict]:
    """Return the last `n` audit records (oldest→newest). Empty if none."""
    try:
        lines = AUDIT_LOG.read_text().splitlines()
    except OSError:
        return []
    out: list[dict] = []
    for line in lines[-n:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out
