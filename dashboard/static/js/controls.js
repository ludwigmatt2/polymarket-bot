export class Controls {
  constructor(containerId, { onScan, onStart, onStop, onInterval }) {
    this._el = document.getElementById(containerId);
    this._onScan     = onScan;
    this._onStart    = onStart;
    this._onStop     = onStop;
    this._onInterval = onInterval;
    this._isScanning = false;
    this._scanCount  = 0;
    this._render();
  }

  update({ is_scanning, scan_interval, scan_count }) {
    this._isScanning = is_scanning;
    this._scanCount  = scan_count;

    const dot    = document.getElementById("ctrl-dot");
    const label  = document.getElementById("ctrl-state-label");
    const toggleBtn = document.getElementById("ctrl-toggle");
    const counter   = document.getElementById("ctrl-scan-counter");
    const slider    = document.getElementById("ctrl-interval");
    const sliderVal = document.getElementById("ctrl-interval-val");

    if (dot) {
      dot.className = is_scanning ? "scanning" : "";
      dot.style.background = is_scanning ? "var(--green)" : "var(--text-dim)";
    }
    if (label)  label.textContent = is_scanning ? "SCANNING" : "IDLE";
    if (toggleBtn) {
      toggleBtn.textContent = is_scanning ? "⏹ Stop Loop" : "▶ Start Loop";
      toggleBtn.className   = "btn " + (is_scanning ? "danger" : "primary");
    }
    if (counter) counter.textContent = `Scans: ${scan_count}`;
    if (slider && scan_interval) {
      slider.value = scan_interval;
      if (sliderVal) sliderVal.textContent = _fmtInterval(scan_interval);
    }
  }

  _render() {
    this._el.innerHTML = `
      <div class="ctrl-group">
        <div class="ctrl-label">Status</div>
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
          <div id="ctrl-dot" style="width:8px;height:8px;border-radius:50%;background:var(--text-dim)"></div>
          <span id="ctrl-state-label" style="font-size:11px;letter-spacing:.08em;color:var(--text-dim)">IDLE</span>
        </div>
        <div class="scan-counter" id="ctrl-scan-counter">Scans: 0</div>
      </div>

      <div class="ctrl-group">
        <div class="ctrl-label">Loop</div>
        <button class="btn primary" id="ctrl-toggle">▶ Start Loop</button>
      </div>

      <div class="ctrl-group">
        <div class="ctrl-label">Manual</div>
        <button class="btn" id="ctrl-scan">🔍 Scan Now</button>
      </div>

      <div class="ctrl-group">
        <div class="ctrl-label">Interval</div>
        <div class="interval-wrap">
          <input type="range" id="ctrl-interval" min="60" max="3600" step="60" value="3600" />
          <span class="interval-val" id="ctrl-interval-val">60m</span>
        </div>
      </div>
    `;

    document.getElementById("ctrl-scan").addEventListener("click", async () => {
      const btn = document.getElementById("ctrl-scan");
      btn.disabled = true;
      btn.textContent = "Scanning…";
      await this._onScan();
      setTimeout(() => {
        btn.disabled = false;
        btn.textContent = "🔍 Scan Now";
      }, 2000);
    });

    document.getElementById("ctrl-toggle").addEventListener("click", async () => {
      if (this._isScanning) await this._onStop();
      else await this._onStart();
    });

    const slider = document.getElementById("ctrl-interval");
    const sliderVal = document.getElementById("ctrl-interval-val");
    slider.addEventListener("input", () => {
      sliderVal.textContent = _fmtInterval(parseInt(slider.value));
    });
    slider.addEventListener("change", () => {
      this._onInterval(parseInt(slider.value));
    });
  }
}

function _fmtInterval(s) {
  if (s < 60)   return `${s}s`;
  if (s < 3600) return `${s / 60}m`;
  return `${s / 3600}h`;
}
