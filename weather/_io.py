"""
Atomic file-write helpers.

Both functions write to a .tmp sibling then call os.replace(), which is
atomic on POSIX (rename is guaranteed to be atomic by the kernel).
"""

from __future__ import annotations

import csv
import json
import os
from pathlib import Path


def atomic_write_text(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def atomic_write_json(path: Path, data: object) -> None:
    atomic_write_text(path, json.dumps(data, indent=2))


def atomic_write_csv(path: Path, headers: list[str], rows: list[dict]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp, path)
