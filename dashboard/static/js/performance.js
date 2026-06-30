const GATE_LABELS = {
  "need_": "≥20 resolved trades",
  "profit_factor_": "Profit factor ≥1.5",
  "bss_": "Brier skill score ≥0",
  "drawdown_": "Max drawdown ≤20%",
};

function gateKey(reason) {
  for (const k of Object.keys(GATE_LABELS)) {
    if (reason.startsWith(k)) return k;
  }
  return reason;
}

function colorClass(val, good, bad) {
  if (val >= good) return "green";
  if (val <= bad)  return "red";
  return "amber";
}

export class PerformancePanel {
  constructor(containerId) {
    this._el = document.getElementById(containerId);
    this._render(null);
  }

  update(stats) {
    this._render(stats);
  }

  _render(s) {
    if (!s || s.total_trades === 0) {
      this._el.innerHTML = `
        <div class="empty-state" style="height:80px">
          <div style="font-size:18px">📊</div>
          <div>No trades yet</div>
        </div>`;
      return;
    }

    const wrPct   = (s.win_rate * 100).toFixed(1);
    const pnl     = s.total_paper_pnl.toFixed(2);
    const pf      = s.profit_factor === Infinity ? "∞" : s.profit_factor.toFixed(2);
    const bss     = s.brier_skill_score.toFixed(3);
    const edge    = (s.avg_edge_pp * 100).toFixed(1);

    // BSS gauge: map [-1, 1] to [0, 100%]
    const bssNum    = parseFloat(s.brier_skill_score);
    const bssFill   = Math.max(0, Math.min(100, (bssNum + 1) / 2 * 100)).toFixed(1);

    const gateAll = [
      { key: "need_",        pass: s.resolved_trades >= 20, label: `${s.resolved_trades}/20 resolved` },
      { key: "profit_factor_", pass: s.profit_factor >= 1.5, label: `PF ${pf} ≥ 1.5` },
      { key: "bss_",         pass: bssNum >= 0,             label: `BSS ${bss} ≥ 0` },
      { key: "drawdown_",    pass: s.max_drawdown_pct <= 0.2, label: `DD ${(s.max_drawdown_pct*100).toFixed(1)}% ≤ 20%` },
    ];

    const gateHtml = gateAll.map(g => `
      <div class="gate-item ${g.pass ? "pass" : "fail"}">
        <span class="icon">${g.pass ? "✓" : "○"}</span>
        <span>${g.label}</span>
      </div>
    `).join("");

    const liveBanner = s.ready_for_live
      ? `<div class="live-ready-banner">🟢 LIVE READY</div>`
      : "";

    this._el.innerHTML = `
      <div class="stat-block">
        <div class="stat-label">Win Rate</div>
        <div class="stat-value ${colorClass(s.win_rate, 0.55, 0.45)}">${wrPct}%</div>
        <div class="stat-sub">${s.resolved_trades} resolved / ${s.total_trades} total</div>
      </div>

      <div class="stat-block">
        <div class="stat-label">Paper PnL</div>
        <div class="stat-value ${pnl >= 0 ? "green" : "red"}">${pnl >= 0 ? "+" : ""}$${pnl}</div>
        <div class="stat-sub">avg edge ${edge}pp</div>
      </div>

      <div class="stat-block">
        <div class="stat-label">Brier Skill Score</div>
        <div class="stat-value ${colorClass(bssNum, 0.05, 0)}">${bss}</div>
        <div class="bss-gauge-wrap">
          <div class="bss-zero-line"></div>
          <div class="bss-gauge-fill" style="width:${bssFill}%"></div>
        </div>
        <div class="stat-sub">mean Brier ${s.mean_brier_score.toFixed(4)} · 0.25=random</div>
      </div>

      <div class="stat-block">
        <div class="stat-label">Profit Factor</div>
        <div class="stat-value ${colorClass(s.profit_factor, 1.5, 1)}">${pf}</div>
        <div class="stat-sub">max DD ${(s.max_drawdown_pct*100).toFixed(1)}%</div>
      </div>

      <div class="stat-block">
        <div class="stat-label">Go-Live Gate</div>
        <div class="gate-list">${gateHtml}</div>
        ${liveBanner}
      </div>
    `;
  }
}
