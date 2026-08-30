// Correction v1.2 #7: NEVER use innerHTML with data that came from the
// backend (justificativas, motivos, resumos de IA, mensagens de erro) --
// all of it is inserted via textContent / DOM element creation only, so an
// externally-supplied string (e.g. from a future real AI provider) can
// never be interpreted as markup or an executable event handler.
const $ = (id) => document.getElementById(id);

const DIRECTION_LABELS = { BUY: "COMPRA", SELL: "VENDA", HOLD: "AGUARDAR" };
const MODE_LABELS = { REPLAY: "REPLAY (simulado)", PAPER_LOCAL: "PAPER_LOCAL (simulado)", BYBIT_DEMO: "BYBIT_DEMO (Bybit Demo Trading)" };

function translateDirection(direction) {
  return DIRECTION_LABELS[direction] || direction;
}

function fmtNumber(v, digits = 2) {
  if (v === "indisponível" || v === null || v === undefined) return "indisponível";
  if (typeof v === "number") return v.toFixed(digits);
  return String(v);
}

function isUnavailable(v) {
  return v === "indisponível" || v === null || v === undefined;
}

function pnlClass(v) {
  if (typeof v !== "number") return "";
  return v > 0 ? "positive" : v < 0 ? "negative" : "";
}

async function getJSON(url, opts) {
  const res = await fetch(url, opts);
  return res.json();
}

function clearChildren(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

// Builds one <div class="kv"><span>label</span><span class="v ...">value</span></div>
// entirely via textContent -- no markup ever passes through as HTML.
function kvRow(container, label, value, extraClass) {
  const row = document.createElement("div");
  row.className = "kv";

  const labelSpan = document.createElement("span");
  labelSpan.textContent = label;

  const valueSpan = document.createElement("span");
  valueSpan.className = "v" + (extraClass ? ` ${extraClass}` : "") + (isUnavailable(value) ? " unavailable" : "");
  valueSpan.textContent = value;

  row.appendChild(labelSpan);
  row.appendChild(valueSpan);
  container.appendChild(row);
}

// Builds a <tr> from an array of cell descriptors: string | {text, className}.
function buildRow(cells) {
  const tr = document.createElement("tr");
  cells.forEach((cell) => {
    const td = document.createElement("td");
    if (cell && typeof cell === "object") {
      td.textContent = cell.text;
      if (cell.className) td.className = cell.className;
    } else {
      td.textContent = cell;
    }
    tr.appendChild(td);
  });
  return tr;
}

function setRows(tbody, rowsData) {
  clearChildren(tbody);
  rowsData.forEach((cells) => tbody.appendChild(buildRow(cells)));
}

async function refreshState() {
  const s = await getJSON("/api/state");
  $("chip-mode").textContent = `MODO: ${MODE_LABELS[s.mode] || s.mode}`;
  $("chip-conn").textContent = `CONEXÃO: ${s.mode === "REPLAY" ? "offline (replay)" : "ativa"}`;
  $("chip-trading").textContent = `OPERAÇÕES: ${s.trading_blocked ? "BLOQUEADAS" : "ATIVAS"}`;
  $("chip-kill").textContent = `BLOQUEIO DE EMERGÊNCIA: ${s.kill_switch_engaged ? "ATIVADO" : "desativado"}`;
  $("last-updated").textContent = new Date().toLocaleString("pt-BR");
}

async function refreshMetrics() {
  const m = await getJSON("/api/metrics");

  const metricsBox = $("metrics-box");
  clearChildren(metricsBox);
  kvRow(metricsBox, "Operações encerradas", m.closed_trades_count);
  kvRow(metricsBox, "Taxa de acerto", fmtNumber(m.win_rate, 3));
  kvRow(metricsBox, "Payoff", fmtNumber(m.payoff));
  kvRow(metricsBox, "Expectativa", fmtNumber(m.expectancy));

  const pnlBox = $("pnl-box");
  clearChildren(pnlBox);
  kvRow(pnlBox, "Lucro bruto", fmtNumber(m.gross_profit), pnlClass(m.gross_profit));
  kvRow(pnlBox, "Prejuízo bruto", fmtNumber(m.gross_loss), pnlClass(m.gross_loss));
  kvRow(pnlBox, "Lucro líquido", fmtNumber(m.net_profit), pnlClass(m.net_profit));
  kvRow(pnlBox, "Comissões", fmtNumber(m.commissions));
  kvRow(pnlBox, "Taxa de financiamento (Funding)", fmtNumber(m.funding));

  const riskBox = $("risk-metrics-box");
  clearChildren(riskBox);
  kvRow(riskBox, "Fator de lucro (Profit Factor)", fmtNumber(m.profit_factor));
  kvRow(riskBox, "Rebaixamento máx. ($) (Drawdown)", fmtNumber(m.max_drawdown_money));
  kvRow(riskBox, "Rebaixamento máx. (%) (Drawdown)", fmtNumber(m.max_drawdown_pct));
  kvRow(riskBox, "Retorno/Rebaixamento", fmtNumber(m.return_over_drawdown));
  kvRow(riskBox, "Exposição (USD)", fmtNumber(m.exposure_usd));
}

async function refreshAccount() {
  const positions = await getJSON("/api/positions");
  const totalExposure = positions.reduce((acc, p) => acc + p.qty * p.avg_entry_price, 0);

  const accountBox = $("account-box");
  clearChildren(accountBox);
  kvRow(accountBox, "Saldo inicial (demo)", "1000.00");
  kvRow(accountBox, "Posições abertas", positions.length);
  kvRow(accountBox, "Exposição aberta", totalExposure.toFixed(2));

  setRows(
    document.querySelector("#positions-table tbody"),
    positions.map((p) => [
      p.symbol,
      translateDirection(p.side),
      p.qty.toFixed(6),
      p.avg_entry_price.toFixed(2),
      p.stop_loss != null ? p.stop_loss.toFixed(2) : "-",
      p.take_profit != null ? p.take_profit.toFixed(2) : "-",
    ])
  );
}

async function refreshSignals() {
  const rows = await getJSON("/api/signals?limit=20");
  setRows(
    document.querySelector("#signals-table tbody"),
    rows.map((r) => [
      new Date(r.created_at).toLocaleString("pt-BR"),
      translateDirection(r.direction),
      r.observed_price.toFixed(2),
      r.justification,
    ])
  );
}

async function refreshRisk() {
  const rows = await getJSON("/api/risk-evaluations?limit=20");
  setRows(
    document.querySelector("#risk-table tbody"),
    rows.map((r) => [
      new Date(r.created_at).toLocaleString("pt-BR"),
      { text: r.approved ? "APROVADO" : "REJEITADO", className: r.approved ? "positive" : "negative" },
      r.reason,
    ])
  );
}

async function refreshAI() {
  const rows = await getJSON("/api/ai-recommendations?limit=20");
  setRows(
    document.querySelector("#ai-table tbody"),
    rows.map((r) => [
      new Date(r.created_at).toLocaleString("pt-BR"),
      translateDirection(r.recommendation),
      r.confidence.toFixed(2),
      r.reasoning_summary,
    ])
  );
}

async function refreshFailures() {
  const rows = await getJSON("/api/failures?limit=20");
  setRows(
    document.querySelector("#failures-table tbody"),
    rows.map((r) => [new Date(r.created_at).toLocaleString("pt-BR"), r.kind, r.detail])
  );
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
  const values = points.map((p) => p.equity);
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
  const res = await getJSON("/api/kill-switch/engage", { method: "POST" });
  if (res.mensagem) $("status-message").textContent = res.mensagem;
  refreshAll();
});
$("btn-unkill").addEventListener("click", async () => {
  const res = await getJSON("/api/kill-switch/disengage", { method: "POST" });
  if (res.mensagem) $("status-message").textContent = res.mensagem;
  refreshAll();
});

refreshAll();
setInterval(refreshAll, 2000);
