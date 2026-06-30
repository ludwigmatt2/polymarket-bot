import { GlobeScene }      from "./globe.js";
import { SignalFeed }       from "./signals.js";
import { PerformancePanel } from "./performance.js";
import { Controls }         from "./controls.js";

const API = "http://localhost:8765/api";
const WS_URL = `ws://${location.host}/ws`;

// ── Init modules ──────────────────────────────────────────────────────────────
const globe = new GlobeScene(document.getElementById("globe-canvas"));

// ── City detail panel ─────────────────────────────────────────────────────────
const cityDetail = document.getElementById("city-detail");
const cityDetailContent = document.getElementById("city-detail-content");

globe.setClickCallback((signal) => {
  const dirClass = signal.direction === "YES" ? "yes" : "no";
  cityDetailContent.innerHTML = `
    <div class="city-detail-name">${signal.city}, ${signal.country}</div>
    <div class="signal-row" style="margin-bottom:10px">
      <span class="badge ${dirClass}">${signal.direction}</span>
      <span style="font-size:11px;color:var(--text-dim);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${signal.title}</span>
    </div>
    <div class="city-detail-row">Edge <span>${(signal.edge_pp * 100).toFixed(1)} pp</span></div>
    <div class="city-detail-row">Market P <span>${(signal.market_p * 100).toFixed(0)}%</span></div>
    <div class="city-detail-row">Model P <span>${(signal.model_p * 100).toFixed(0)}%</span></div>
    <div class="city-detail-row">Confidence <span>${(signal.confidence_score * 100).toFixed(0)}%</span></div>
    <div class="city-detail-row">Liquidity <span>$${signal.liquidity_usd != null ? Number(signal.liquidity_usd).toLocaleString() : "—"}</span></div>
    ${signal.url ? `<a class="city-detail-link" href="${signal.url}" target="_blank" rel="noopener">Open on Polymarket →</a>` : ""}
  `;
  cityDetail.classList.remove("hidden");
});

document.getElementById("city-detail-close").addEventListener("click", () => {
  cityDetail.classList.add("hidden");
});

const feed  = new SignalFeed("signal-feed");
const perf  = new PerformancePanel("perf-body");
const ctrl  = new Controls("ctrl-body", {
  onScan:     () => fetch(`${API}/scan`,     { method: "POST" }),
  onStart:    () => fetch(`${API}/start`,    { method: "POST" }),
  onStop:     () => fetch(`${API}/stop`,     { method: "POST" }),
  onInterval: (s) => fetch(`${API}/interval`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ seconds: s }),
  }),
});

// ── Header helpers ────────────────────────────────────────────────────────────
const statusDot   = document.getElementById("status-dot");
const statusLabel = document.getElementById("status-label");
const lastScanEl  = document.getElementById("last-scan-label");

function updateHeader(isScanning, lastScanAt) {
  statusDot.className   = isScanning ? "scanning" : "";
  statusLabel.textContent = isScanning ? "SCANNING" : "IDLE";
  if (lastScanAt) {
    const ago = _relTime(lastScanAt);
    lastScanEl.textContent = `Last scan: ${ago}`;
  }
}

// Update "N min ago" every 30s
setInterval(() => {
  const t = lastScanEl.dataset.scanAt;
  if (t) lastScanEl.textContent = `Last scan: ${_relTime(t)}`;
}, 30000);

function _relTime(iso) {
  const diffSec = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (diffSec < 60)  return "just now";
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)}m ago`;
  return `${Math.floor(diffSec / 3600)}h ago`;
}

// ── Hydrate on load ──────────────────────────────────────────────────────────
async function hydrate() {
  try {
    const [statusRes, signalsRes, statsRes] = await Promise.all([
      fetch(`${API}/status`).then(r => r.json()),
      fetch(`${API}/signals`).then(r => r.json()),
      fetch(`${API}/stats`).then(r => r.json()),
    ]);

    updateHeader(statusRes.is_scanning, statusRes.last_scan_at);
    if (statusRes.last_scan_at) lastScanEl.dataset.scanAt = statusRes.last_scan_at;

    ctrl.update({
      is_scanning:   statusRes.is_scanning,
      scan_interval: statusRes.scan_interval,
      scan_count:    statusRes.scan_count,
    });

    if (signalsRes.length > 0) {
      feed.update(signalsRes);
      globe.updateCityMarkers(signalsRes);
    }
    if (statsRes && Object.keys(statsRes).length > 0) {
      perf.update(statsRes);
    }
  } catch (e) {
    console.warn("Hydration failed:", e);
  }
}

hydrate();

// ── WebSocket connection ──────────────────────────────────────────────────────
let _ws;
let _retryDelay = 1000;
const overlay = document.getElementById("reconnect-overlay");

function connect() {
  _ws = new WebSocket(WS_URL);

  _ws.onopen = () => {
    _retryDelay = 1000;
    overlay.classList.remove("visible");
  };

  _ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    handleMessage(msg);
  };

  _ws.onclose = () => {
    overlay.classList.add("visible");
    setTimeout(connect, _retryDelay);
    _retryDelay = Math.min(_retryDelay * 2, 15000);
  };
}

function handleMessage(msg) {
  switch (msg.type) {
    case "scan_started":
      feed.setLoading();
      updateHeader(true, null);
      ctrl.update({ is_scanning: true, scan_interval: null, scan_count: msg.scan_count });
      break;

    case "scan_result":
      updateHeader(false, msg.scan_time);
      if (msg.scan_time) lastScanEl.dataset.scanAt = msg.scan_time;
      feed.update(msg.signals);
      perf.update(msg.stats);
      globe.updateCityMarkers(msg.signals || []);
      ctrl.update({ is_scanning: false, scan_interval: null, scan_count: msg.scan_count });
      break;

    case "scan_error":
      console.error("Scan error:", msg.message);
      updateHeader(false, null);
      ctrl.update({ is_scanning: false, scan_interval: null, scan_count: null });
      break;

    case "status":
      updateHeader(msg.is_scanning, msg.last_scan_at);
      ctrl.update({
        is_scanning:   msg.is_scanning,
        scan_interval: msg.scan_interval,
        scan_count:    msg.scan_count,
      });
      break;
  }
}

connect();
