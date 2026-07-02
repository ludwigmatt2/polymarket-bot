"""Tests for the append-only audit log (weather/audit.py) — SECURITY_PLAN Phase F."""

import json

import pytest

from weather import audit as A


@pytest.fixture
def audit_path(monkeypatch, tmp_path):
    p = tmp_path / "audit.jsonl"
    monkeypatch.setattr(A, "AUDIT_LOG", p)
    return p


def test_appends_one_line_per_call(audit_path):
    A.audit_log("withdrawal", actor=1, amount=5.0, to="0xabc")
    A.audit_log("mode_live", actor=2)
    lines = audit_path.read_text().splitlines()
    assert len(lines) == 2
    r0 = json.loads(lines[0])
    assert r0["action"] == "withdrawal" and r0["actor"] == 1
    assert r0["amount"] == 5.0 and r0["to"] == "0xabc"
    assert "ts" in r0
    r1 = json.loads(lines[1])
    assert r1["action"] == "mode_live" and r1["actor"] == 2


def test_actor_optional(audit_path):
    A.audit_log("startup_selfcheck", issues=0)
    r = json.loads(audit_path.read_text().splitlines()[0])
    assert "actor" not in r
    assert r["action"] == "startup_selfcheck" and r["issues"] == 0


def test_read_audit_returns_last_n(audit_path):
    for i in range(10):
        A.audit_log("evt", actor=i)
    last3 = A.read_audit(3)
    assert [r["actor"] for r in last3] == [7, 8, 9]


def test_read_audit_empty_when_missing(audit_path):
    assert A.read_audit() == []


def test_read_audit_skips_corrupt_lines(audit_path):
    A.audit_log("ok", actor=1)
    with audit_path.open("a") as f:
        f.write("not json\n")
    A.audit_log("ok2", actor=2)
    recs = A.read_audit()
    assert [r["action"] for r in recs] == ["ok", "ok2"]


def test_non_serializable_detail_does_not_raise(audit_path):
    # default=str keeps a stray object from blowing up the audit write.
    A.audit_log("weird", actor=1, obj=object())
    assert len(audit_path.read_text().splitlines()) == 1
