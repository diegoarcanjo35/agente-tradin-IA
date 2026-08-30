const $ = (id) => document.getElementById(id);

function fmt(v, digits = 2) {
  if (v === "indisponível" || v === null || v === undefined) return '<span class="unavailable">indisponível</span>';
  if (typeof v === "number") return v.toFixed(digits);
  return String(v);
}

function pnlClass(v) {
  if (typeof v !== "number") return "";
  return v > 0 ? "positive" : v < 0 ? "negative" : "";
}

async function getJSON(url, opts) {
  const res = await fetch(url, opts);
  return res.json();
}

async function refreshState() {
  const s = await getJSON("/api/state");
  $("chip-mode").textContent = `MODO: ${s.mode}`;
  $("chip-conn").textContent = `CONEXÃO: ${s.mode === "REPLAY" ? "offline (replay)" : "ativa"}`;
  $("chip-trading").textContent = `TRADING: ${s.trading_blocked ? "BLOQUEADO" : "ATIVO"}`;
  $("chip-kill").textContent = `KILL SWITCH: ${s.kill_switch_engaged ? "ENGATADO" : "livre"}`;
  $("last-updated").textContent = new Date().toISOString();
}

async function refreshMetrics() {
  const m = await getJSON("/api/metrics");
  $("metrics-box").innerHTML = `
    <div class="kv"><span>Operações encerradas</span><span class="v">${m.closed_trades_count}</span></div>
    <div class="kv"><span>Taxa de acerto</span><span class="v">${fmt(m.win_rate, 3)}</span></div>
    <div class="kv"><span>Payoff</span><span class="v">${fmt(m.payoff)}</span></div>
    <div class="kv"><span>Expectativa</span><span class="v">${fmt(m.expectancy)}</span></div>
  `;
  $("pnl-box").innerHTML = `
    <div class="kv"><span>Lucro bruto</span><span class="v ${pnlClass(m.gross_profit)}">${fmt(m.gross_profit)}</span></div>
    <div class="kv"><span>Prejuízo bruto</span><span class="v ${pnlClass(m.gross_loss)}">${fmt(m.gross_loss)}</span></div>
    <div class="kv"><span>Lucro líquido</span><span class="v ${pnlClass(m.net_profit)}">${fmt(m.net_profit)}</span></div>
    <div class="kv"><span>Comissões</span><span class="v">${fmt(m.commissions)}</span></div>
    <div class="kv"><span>Funding</span><span class="v">${fmt(m.funding)}</span></div>
  `;
  $("risk-metrics-box").innerHTML = `
    <div class="kv"><span>Profit factor</span><span class="v">${fmt(m.profit_factor)}</span></div>
    <div class="kv"><span>Drawdown máx ($)</span><span class="v">${fmt(m.max_drawdown_money)}</span></div>
    <div class="kv"><span>Drawdown máx (%)</span><span class="v">${fmt(m.max_drawdown_pct)}</span></div>
    <div class="kv"><span>Retorno/Drawdown</span><span class="v">${fmt(m.return_over_drawdown)}</span></div>
    <div class="kv"><span>Exposição (USD)</span><span class="v">${fmt(m.exposure_usd)}</span></div>
  `;
}

async function refreshAccount() {
  const positions = await getJSON("/api/positions");
  const totalExposure = positions.reduce((acc, p) => acc + p.qty * p.avg_entry_price, 0);
  $("account-box").innerHTML = `
    <div class="kv"><span>Saldo inicial (demo)</span><span class="v">1000.00</span></div>
    <div class="kv"><span>Posições abertas</span><span class="v">${positions.length}</span></div>
    <div class="kv"><span>Exposição aberta</span><span class="v">${totalExposure.toFixed(2)}</span></div>
  `;

  const tbody = document.querySelector("#positions-table tbody");
  tbody.innerHTML = positions.map(p => `
    <tr><td>${p.symbol}</td><td>${p.side}</td><td>${p.qty.toFixed(6)}</td>
    <td>${p.avg_entry_price.toFixed(2)}</td><td>${p.stop_loss?.toFixed(2) ?? "-"}</td>
    <td>${p.take_profit?.toFixed(2) ?? "-"}</td></tr>
  `).join("");
}

async function refreshSignals() {
  const rows = await getJSON("/api/signals?limit=20");
  const tbody = document.querySelector("#signals-table tbody");
  tbody.innerHTML = rows.map(r => `
    <tr><td>${r.created_at}</td><td>${r.direction}</td><td>${r.observed_price.toFixed(2)}</td>
    <td>${r.justification}</td></tr>
  `).join("");
}

async function refreshRisk() {
  const rows = await getJSON("/api/risk-evaluations?limit=20");
  const tbody = document.querySelector("#risk-table tbody");
  tbody.innerHTML = rows.map(r => `
    <tr><td>${r.created_at}</td><td class="${r.approved ? 'positive' : 'negative'}">${r.approved}</td>
    <td>${r.reason}</td></tr>
  `).join("");
}

async function refreshAI() {
  const rows = await getJSON("/api/ai-recommendations?limit=20");
  const tbody = document.querySelector("#ai-table tbody");
  tbody.innerHTML = rows.map(r => `
    <tr><td>${r.created_at}</td><td>${r.recommendation}</td>
    <td>${r.confidence.toFixed(2)}</td><td>${r.reasoning_summary}</td></tr>
  `).join("");
}

async function refreshFailures() {
  const rows = await getJSON("/api/failures?limit=20");
  const tbody = document.querySelector("#failures-table tbody");
  tbody.innerHTML = rows.map(r => `
    <tr><td>${r.created_at}</td><td>${r.kind}</td><td>${r.detail}</td></tr>
  `).join("");
}

async function refreshEquityCurve() {
  const points = await getJSON("/api/equity-curve");
  const canvas = $("equity-canvas");
  const ctx = canvas.getContext("2d");
  const w = canvas.width, h = canvas.height;
  ctx.clearRect(0, 0, w, h);
  if (points.length < 2) {
    ctx.fillStyle = "#8b96ab";
    ctx.fillText("Sem operações encerradas ainda.", 10, h / 2);
    return;
  }
  const values = points.map(p => p.equity);
  const min = Math.min(...values), max = Math.max(...values);
  const pad = 10;
  const scaleX = (w - 2 * pad) / (points.length - 1);
  const scaleY = max === min ? 1 : (h - 2 * pad) / (max - min);

  ctx.strokeStyle = "#60a5fa";
  ctx.lineWidth = 2;
  ctx.beginPath();
  points.forEach((p, i) => {
    const x = pad + i * scaleX;
    const y = h - pad - (p.equity - min) * scaleY;
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  });
  ctx.stroke();
}

async function refreshAll() {
  await Promise.all([
    refreshState(), refreshMetrics(), refreshAccount(), refreshSignals(),
    refreshRisk(), refreshAI(), refreshFailures(), refreshEquityCurve(),
  ]);
}

$("btn-kill").addEventListener("click", async () => {
  await fetch("/api/kill-switch/engage", { method: "POST" });
  refreshAll();
});
$("btn-unkill").addEventListener("click", async () => {
  await fetch("/api/kill-switch/disengage", { method: "POST" });
  refreshAll();
});

refreshAll();
setInterval(refreshAll, 2000);
