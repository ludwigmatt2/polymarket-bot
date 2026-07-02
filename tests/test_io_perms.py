"""atomic_write mode enforcement — sensitive config must land 0600 (Phase F follow-up)."""

import stat

from weather._io import atomic_write_json, atomic_write_text


def test_atomic_write_text_sets_mode(tmp_path):
    p = tmp_path / "x.txt"
    atomic_write_text(p, "hi", mode=0o600)
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert p.read_text() == "hi"


def test_atomic_write_json_sets_mode(tmp_path):
    p = tmp_path / "x.json"
    atomic_write_json(p, {"a": 1}, mode=0o600)
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_atomic_write_rewrite_keeps_mode(tmp_path):
    p = tmp_path / "x.json"
    atomic_write_json(p, {"a": 1}, mode=0o600)
    atomic_write_json(p, {"a": 2}, mode=0o600)  # rewrite must stay 0600
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_atomic_write_without_mode_still_writes(tmp_path):
    p = tmp_path / "y.txt"
    atomic_write_text(p, "data")  # no mode → default umask, must not error
    assert p.read_text() == "data"
