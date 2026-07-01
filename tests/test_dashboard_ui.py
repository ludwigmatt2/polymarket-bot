"""
Playwright E2E tests for the Weather Bot dashboard at http://localhost:8765.

Run:
  pytest tests/test_dashboard_ui.py -v --headed   # visible browser
  pytest tests/test_dashboard_ui.py -v            # headless
"""

import socket
from urllib.parse import urlparse

import pytest

# E2E deps are optional (see requirements.txt). Skip the whole module cleanly
# rather than erroring at collection when they're absent.
pytest.importorskip("pytest_playwright", reason="E2E deps not installed")
pytest.importorskip("playwright.sync_api", reason="E2E deps not installed")
from playwright.sync_api import Page, expect  # noqa: E402


BASE = "http://localhost:8765"


def _server_up(url: str) -> bool:
    p = urlparse(url)
    try:
        with socket.create_connection((p.hostname, p.port or 80), timeout=0.5):
            return True
    except OSError:
        return False


# Live-browser E2E: without the dashboard running these can't pass, so skip.
if not _server_up(BASE):
    pytest.skip(f"dashboard server not running at {BASE}", allow_module_level=True)


@pytest.fixture(scope="session")
def console_errors():
    """Accumulate console errors across the session for the final check."""
    return []


# ── Page load ────────────────────────────────────────────────────────────────

def test_page_loads(page: Page, console_errors):
    errors = []
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)

    response = page.goto(BASE)
    assert response.status == 200

    console_errors.extend(errors)


def test_title(page: Page):
    page.goto(BASE)
    expect(page).to_have_title("Weather Bot · Dashboard")


# ── Header ───────────────────────────────────────────────────────────────────

def test_header_renders(page: Page):
    page.goto(BASE)
    expect(page.locator(".logo")).to_contain_text("WEATHER BOT")
    expect(page.locator("#status-label")).to_have_text("IDLE")


# ── Globe ─────────────────────────────────────────────────────────────────────

def test_globe_canvas_present(page: Page):
    page.goto(BASE)
    canvas = page.locator("#globe-canvas")
    expect(canvas).to_be_visible()
    # Canvas should have non-zero dimensions after Three.js initialises
    page.wait_for_timeout(1500)
    box = canvas.bounding_box()
    assert box is not None
    assert box["width"] > 0 and box["height"] > 0


# ── Panels ────────────────────────────────────────────────────────────────────

def test_signal_feed_empty_state(page: Page):
    page.goto(BASE)
    empty = page.locator("#signal-feed .empty-state")
    expect(empty).to_be_visible()
    expect(empty).to_contain_text("Run a scan")


def test_controls_render(page: Page):
    page.goto(BASE)
    expect(page.locator("#ctrl-scan")).to_be_visible()
    expect(page.locator("#ctrl-toggle")).to_be_visible()
    expect(page.locator("#ctrl-interval")).to_be_visible()
    expect(page.locator("#ctrl-scan-counter")).to_contain_text("Scans: 0")


# ── WebSocket ─────────────────────────────────────────────────────────────────

def test_websocket_connects(page: Page):
    """Reconnect overlay should NOT be visible after WS handshake."""
    page.goto(BASE)
    page.wait_for_timeout(1000)
    overlay = page.locator("#reconnect-overlay")
    # overlay is hidden by default (no 'visible' class); check it's not shown
    assert "visible" not in (overlay.get_attribute("class") or "")


# ── Scan trigger ──────────────────────────────────────────────────────────────

def test_scan_now_button_triggers_scan(page: Page):
    """Clicking Scan Now should disable the button and fire POST /api/scan."""
    scan_fired = []

    page.on(
        "request",
        lambda req: scan_fired.append(req.url)
        if "/api/scan" in req.url and req.method == "POST"
        else None,
    )

    page.goto(BASE)
    page.wait_for_timeout(500)

    btn = page.locator("#ctrl-scan")
    expect(btn).to_be_enabled()
    btn.click()

    # Button should go disabled+text change immediately
    expect(btn).to_be_disabled()
    expect(btn).to_contain_text("Scanning")

    # API request should have fired
    page.wait_for_timeout(1000)
    assert any("/api/scan" in u for u in scan_fired), "POST /api/scan was not called"


def test_scan_counter_increments(page: Page):
    """After a scan, the scan counter should show Scans: 1."""
    page.goto(BASE)
    page.wait_for_timeout(500)
    page.locator("#ctrl-scan").click()
    # Wait for WS scan_started message to update counter
    expect(page.locator("#ctrl-scan-counter")).to_have_text("Scans: 1", timeout=5000)


# ── API endpoints (lightweight smoke) ────────────────────────────────────────

def test_api_status(page: Page):
    resp = page.request.get(f"{BASE}/api/status")
    assert resp.status == 200
    data = resp.json()
    assert "is_scanning" in data
    assert "scan_interval" in data


def test_api_signals(page: Page):
    resp = page.request.get(f"{BASE}/api/signals")
    assert resp.status == 200
    assert isinstance(resp.json(), list)


def test_api_stats(page: Page):
    resp = page.request.get(f"{BASE}/api/stats")
    assert resp.status == 200


def test_api_trades(page: Page):
    resp = page.request.get(f"{BASE}/api/trades")
    assert resp.status == 200
    assert isinstance(resp.json(), list)


# ── No JS console errors ──────────────────────────────────────────────────────

def test_no_console_errors_on_load(page: Page):
    errors = []
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    page.goto(BASE)
    page.wait_for_timeout(2000)
    # Filter out known benign third-party noise
    real_errors = [e for e in errors if "favicon" not in e.lower()]
    assert real_errors == [], f"Console errors: {real_errors}"
