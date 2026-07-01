"""
Playwright E2E tests for the Weather Bot dashboard at http://localhost:8765.

Run:
  pytest tests/test_dashboard_ui.py -v --headed   # visible browser
  pytest tests/test_dashboard_ui.py -v            # headless
"""

import pytest

# E2E deps are optional (see requirements.txt). Skip the whole module cleanly
# rather than erroring at collection when they're absent.
pytest.importorskip("pytest_playwright", reason="E2E deps not installed")
pytest.importorskip("playwright.sync_api", reason="E2E deps not installed")
from playwright.sync_api import Page, expect  # noqa: E402


BASE = "http://localhost:8765"


@pytest.fixture(autouse=True)
def _ensure_dashboard(dashboard_server):
    """Start (or reuse) a dashboard server for every test in this module.

    Uses the session-scoped fixture in conftest.py; a fresh server means clean
    scan-counter state, and tests self-skip if E2E deps or the server are absent."""
    return dashboard_server


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
    # Counter renders; don't assert an absolute value (scan_count is server-global
    # and accumulates across the server's lifetime).
    expect(page.locator("#ctrl-scan-counter")).to_contain_text("Scans:")


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


def _scan_count(page: Page) -> int:
    return int(page.locator("#ctrl-scan-counter").inner_text().split(":")[1].strip())


def test_scan_counter_increments(page: Page):
    """Triggering a scan bumps the server-side scan counter by one.

    The counter increments synchronously when the scan is triggered (before the
    scan itself runs), so we assert the delta after a reload — which rehydrates
    the counter from the server's on-connect status. This is robust to scan
    duration and to the scan_count being server-global/accumulating."""
    page.goto(BASE)
    page.wait_for_timeout(500)
    before = _scan_count(page)
    page.locator("#ctrl-scan").click()
    page.wait_for_timeout(1500)  # let POST /api/scan reach the server (count bumps immediately)
    page.reload()
    page.wait_for_timeout(500)
    assert _scan_count(page) == before + 1


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
