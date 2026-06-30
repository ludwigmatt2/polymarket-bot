const METRIC_LABELS = {
  temperature_2m_max: "Max Temp",
  temperature_2m_min: "Min Temp",
  precipitation_sum:  "Precip",
};

function fmt(v, suffix = "") {
  if (v == null) return "—";
  return (v * 100).toFixed(1) + "%" + suffix;
}

function metricLabel(metric, threshold, threshHigh, dirMarket) {
  const base = METRIC_LABELS[metric] || metric;
  if (dirMarket === "range" && threshHigh != null) {
    return `${base} ${threshold}–${threshHigh}`;
  }
  return `${base} ${dirMarket} ${threshold}`;
}

function buildCard(signal) {
  const card = document.createElement("div");
  card.className = "signal-card" + (signal.quality_gate_passed ? "" : " rejected");

  const edgePct = Math.min(signal.edge_pp * 100, 50); // cap at 50pp for bar
  const edgePctDisplay = (signal.edge_pp * 100).toFixed(1);

  if (signal.quality_gate_passed) {
    card.innerHTML = `
      <div class="signal-city">${signal.city}</div>
      <div class="signal-metric">${metricLabel(signal.metric, signal.threshold, signal.threshold_high, signal.direction_market)}</div>
      <div class="signal-row">
        <span class="badge ${signal.direction.toLowerCase()}">${signal.direction}</span>
        <div class="edge-bar-wrap"><div class="edge-bar-fill" style="width:${edgePct * 2}%"></div></div>
        <span class="edge-label">${edgePctDisplay}pp</span>
      </div>
      <div class="prob-row">
        <span>MODEL <span class="prob-val">${(signal.model_p * 100).toFixed(1)}%</span></span>
        <span>↔</span>
        <span>MKT <span class="prob-val">${(signal.market_p * 100).toFixed(1)}%</span></span>
        <span>CONF <span class="prob-val">${(signal.confidence_score * 100).toFixed(0)}%</span></span>
      </div>
    `;
  } else {
    card.innerHTML = `
      <div class="signal-city">${signal.city}</div>
      <div class="signal-metric">${metricLabel(signal.metric, signal.threshold, signal.threshold_high, signal.direction_market)}</div>
      <div class="rejection-reason">✗ ${signal.rejection_reason || "rejected"}</div>
    `;
  }

  return card;
}

export class SignalFeed {
  constructor(containerId) {
    this._el = document.getElementById(containerId);
  }

  update(signals) {
    if (!signals || signals.length === 0) {
      this._el.innerHTML = `<div class="empty-state"><div>No markets found in last scan</div></div>`;
      return;
    }

    const passed   = signals.filter(s => s.quality_gate_passed);
    const rejected = signals.filter(s => !s.quality_gate_passed);

    this._el.innerHTML = "";

    passed.sort((a, b) => b.edge_pp - a.edge_pp).forEach(s => {
      this._el.appendChild(buildCard(s));
    });

    if (rejected.length > 0) {
      const label = document.createElement("div");
      label.className = "rejected-section-label";
      label.textContent = `Rejected (${rejected.length})`;
      this._el.appendChild(label);
      rejected.slice(0, 15).forEach(s => this._el.appendChild(buildCard(s)));
    }
  }

  setLoading() {
    this._el.innerHTML = `<div class="empty-state"><div class="spinner"></div><div>Scanning markets…</div></div>`;
  }
}
