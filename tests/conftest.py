"""Shared pytest fixtures.

Dashboard E2E tests need a running dashboard server. This session fixture starts
a FRESH one (clean state → reliable scan-counter assertions) and tears it down,
unless a server is already listening on the port (then it just reuses that).
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

DASHBOARD_HOST = "127.0.0.1"
DASHBOARD_PORT = 8765
DASHBOARD_URL = f"http://{DASHBOARD_HOST}:{DASHBOARD_PORT}"
_PROJECT_ROOT = Path(__file__).parent.parent


def _port_open(host: str, port: int, timeout: float = 0.4) -> bool:
    with socket.socket() as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


@pytest.fixture(scope="session")
def dashboard_server() -> str:
    """Ensure a dashboard server is reachable for the test session.

    - If one is already running on the port, reuse it (don't manage lifecycle).
    - Otherwise, if playwright is installed, spawn a fresh uvicorn server for the
      session and tear it down afterwards.
    - If E2E deps aren't installed, skip any test that requests this fixture.
    """
    if _port_open(DASHBOARD_HOST, DASHBOARD_PORT):
        yield DASHBOARD_URL
        return

    if not (_module_available("playwright") and _module_available("uvicorn")):
        pytest.skip("dashboard E2E deps not installed")

    env = {
        **os.environ,
        "POLYMARKET_BOT_TOKEN": os.environ.get("POLYMARKET_BOT_TOKEN", "test:token"),
        "TELEGRAM_ADMIN_ID": os.environ.get("TELEGRAM_ADMIN_ID", "1"),
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "dashboard.server:app",
         "--host", DASHBOARD_HOST, "--port", str(DASHBOARD_PORT), "--log-level", "warning"],
        cwd=str(_PROJECT_ROOT), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(60):  # up to ~15s to come up
            if _port_open(DASHBOARD_HOST, DASHBOARD_PORT):
                break
            time.sleep(0.25)
        else:
            proc.terminate()
            pytest.skip("dashboard server failed to start")
        yield DASHBOARD_URL
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _module_available(name: str) -> bool:
    import importlib.util
    return importlib.util.find_spec(name) is not None
