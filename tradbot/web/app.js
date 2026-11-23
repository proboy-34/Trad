/* TradBot dashboard - plain JavaScript, no frameworks (fast on weak PCs).
   Only the visible tab is refreshed, and refreshing pauses when the
   browser tab is hidden. */
"use strict";

const $ = (id) => document.getElementById(id);
const LWC = window.LightweightCharts;
const state = { tab: "overview", symbol: null, charts: {}, last: null, symbols: [] };

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (v, d = 2) => (v == null || isNaN(v) ? "–" : "$" + Number(v).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d }));
const price = (v) => (v == null ? "–" : Number(v).toLocaleString(undefined, { maximumFractionDigits: v < 10 ? 4 : 2 }));
const pct = (v, d = 2) => (v == null || isNaN(v) ? "–" : (v > 0 ? "+" : "") + Number(v).toFixed(d) + "%");
const cls = (v) => (v > 0 ? "up" : v < 0 ? "down" : "");
const ago = (t) => {
  if (!t) return "never";
  const s = Math.max(0, Date.now() / 1000 - t);
  if (s < 60) return Math.round(s) + "s ago";
  if (s < 3600) return Math.round(s / 60) + " min ago";
  if (s < 86400) return Math.round(s / 3600) + " h ago";
  return Math.round(s / 86400) + " days ago";
};
const until = (t) => { const s = t - Date.now() / 1000; return s <= 0 ? "now" : s < 3600 ? Math.round(s / 60) + " min" : Math.round(s / 3600) + " h"; };
const dt = (t) => new Date(t * 1000).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });

async function get(path) {
  const r = await fetch("/api/" + path, { cache: "no-store" });
  if (!r.ok) throw new Error(path + " " + r.status);
  return r.json();
}
async function post(path, body = {}) {
  const r = await fetch("/api/" + path, { method: "POST", headers: { "Content-Type": "application/json", "X-TradBot": "1" }, body: JSON.stringify(body) });
  return r.json();
}
function toast(msg) {
  const t = $("toast"); t.textContent = msg; t.classList.remove("hidden");
  clearTimeout(toast.timer); toast.timer = setTimeout(() => t.classList.add("hidden"), 3500);
}

/* ----------------------------------------------------------- charts */
const chartTheme = {
  layout: { background: { type: "solid", color: "transparent" }, textColor: "#8b95a8", fontSize: 12 },
  grid: { vertLines: { color: "rgba(34,44,64,.5)" }, horzLines: { color: "rgba(34,44,64,.5)" } },
  rightPriceScale: { borderColor: "#222c40" },
  timeScale: { borderColor: "#222c40", timeVisible: true, secondsVisible: false },
  crosshair: { mode: 1 },
  autoSize: true,
};
function chart(id) {
  if (!state.charts[id] && LWC) state.charts[id] = { chart: LWC.createChart($(id), chartTheme), series: {} };
  return state.charts[id];
}

/* ------------------------------------------------------------- tabs */
document.querySelectorAll("#tabs button").forEach((b) =>
  b.addEventListener("click", () => {
    document.querySelectorAll("#tabs button").forEach((x) => x.classList.toggle("active", x === b));
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.id === "tab-" + b.dataset.tab));
    state.tab = b.dataset.tab;
    refreshTab();
  }));

/* -------------------------------------------------------- overview */
async function loadState() {
  const s = await get("state");
  state.last = s;
  const sm = s.summary, acc = s.account;
  $("conn").textContent = "updated " + new Date().toLocaleTimeString();
  $("k-equity").textContent = money(sm.equity);
  $("k-start").textContent = "started with " + money(sm.start_balance, 0);
  $("k-total").textContent = pct(sm.total_return_pct); $("k-total").className = cls(sm.total_return_pct);
  $("k-total-usd").textContent = money(sm.equity - sm.start_balance);
  $("k-today").textContent = pct(sm.today_pnl_pct); $("k-today").className = cls(sm.today_pnl_pct);
  $("k-dd").textContent = s.drawdown_pct > 0.005 ? "−" + s.drawdown_pct.toFixed(2) + "%" : "0%";
  $("k-daylimit").textContent = s.risk.daily_loss_limit_pct;
  $("k-kill").textContent = s.risk.kill_switch_drawdown_pct;
  $("btn-pause").textContent = acc.paused ? "Resume bot" : "Pause bot";

  const e = s.engine;
  const status = acc.kill_switch ? '<span class="down">KILL SWITCH ON</span>' : acc.paused ? '<span class="down">Paused</span>' : e.running ? '<span class="up">Running</span>' : "Stopped";
  const rows = [
    ["Status", status],
    ["Last hourly check", ago(e.last_hourly)],
    ["Next decision", e.next_hourly ? "in " + until(e.next_hourly) : "–"],
    ["Active strategies", sm.active_strategies.length || '<span class="down">none yet</span>'],
    ["AI brain", s.brain.ready ? `<span class="up">${esc(s.brain.provider)} on</span>` : '<span class="muted">off</span>'],
    ["Trades finished", sm.total_trades],
  ];
  $("status-list").innerHTML = rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");
  $("prices").innerHTML = s.symbols.map((sym) => `<div class="price"><span>${esc(sym)}</span><b>${price(sm.prices[sym])}</b></div>`).join("");

  $("open-count").textContent = `${s.positions.length} / ${s.risk.max_open_positions} max`;
  $("open-empty").classList.toggle("hidden", s.positions.length > 0);
  $("open-body").innerHTML = s.positions.map((p) => `<tr>
      <td><b>${esc(p.symbol)}</b></td><td>${esc(p.strategy_id)}</td><td>${money(p.cost, 0)}</td>
      <td>${price(p.entry_price)}<div class="muted small">${dt(p.entry_time)}</div></td><td>${price(p.price)}</td>
      <td><span class="down">${price(p.stop)}</span> / <span class="up">${price(p.take)}</span></td>
      <td class="${cls(p.pnl)}">${money(p.pnl)}<div class="small">${pct(p.pnl_pct)}</div></td></tr>`).join("");

  // banners + background job
  const banner = $("banner");
  let msg = "";
  if (acc.kill_switch) msg = "Kill switch is ON: the balance fell too far from its peak, so all new trades are stopped. Review what happened, then reset it in Settings.";
  else if (e.last_error) msg = "Problem: " + e.last_error + " — the bot retries automatically every minute.";
  else if (!sm.active_strategies.length && !s.job.name) msg = "No strategy passed the tests yet, so the bot is not trading (that's the safe choice). The Strategy Lab re-researches every week, or run it now on the Strategies tab.";
  banner.textContent = msg;
  banner.className = "banner" + (msg ? (acc.kill_switch || e.last_error ? "" : " info") : " hidden");
  const j = s.job;
  $("job").classList.toggle("hidden", !j.name);
  if (j.name) { $("job-name").textContent = j.name; $("job-msg").textContent = j.msg; $("job-bar").style.width = j.pct + "%"; }
  $("btn-lab").disabled = $("btn-backtest").disabled = !!j.name;

  if (!state.symbols.length) {
    state.symbols = s.symbols; state.symbol = s.symbols[0];
    $("sym-buttons").innerHTML = s.symbols.map((x) => `<button class="btn" data-sym="${esc(x)}">${esc(x)}</button>`).join("");
    $("sym-buttons").querySelectorAll("button").forEach((b) => b.addEventListener("click", () => { state.symbol = b.dataset.sym; loadCandles(); }));
    $("risk-list").innerHTML = [
      ["Max per trade", s.risk.max_position_pct + "% of balance"], ["Max open trades", s.risk.max_open_positions],
      ["Daily loss stop", s.risk.daily_loss_limit_pct + "%"], ["Kill switch", s.risk.kill_switch_drawdown_pct + "% drop from peak"],
      ["Smallest trade", "$" + s.risk.min_trade_usdt]].map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");
    document.querySelectorAll(".bt-pos").forEach((x) => (x.textContent = s.risk.max_position_pct));
    const f = $("ai-form"); f.provider.value = s.brain.provider;
  }
  const pill = $("ai-pill");
  pill.textContent = s.brain.ready ? `${s.brain.provider} · ${s.brain.model} · ${s.brain.calls_last_hour} calls this hour` : "off — add a key in Settings";
  pill.className = "pill " + (s.brain.ready ? "ok" : "off");
  return s;
}

async function loadEquity() {
  const d = await get("equity");
  const has = d.points.length > 1;
  $("equity-empty").classList.toggle("hidden", has);
  $("equity-chart").classList.toggle("hidden", !has);
  if (!has) return;
  const c = chart("equity-chart"); if (!c) return;
  if (!c.series.eq) c.series.eq = c.chart.addAreaSeries({ lineColor: "#3b82f6", topColor: "rgba(59,130,246,.3)", bottomColor: "rgba(59,130,246,0)", lineWidth: 2, priceFormat: { type: "price", precision: 0, minMove: 1 } });
  const seen = new Set();
  c.series.eq.setData(d.points.map((p) => ({ time: Math.floor(p.time), value: p.equity })).filter((p) => !seen.has(p.time) && seen.add(p.time)));
  if (has) c.chart.timeScale().fitContent();
}

const TAG = { trade: "trade", risk: "risk", error: "error", lab: "lab", system: "system" };
function feedItem(m) {
  const kind = m.kind.startsWith("ai") ? "ai" : TAG[m.kind] || "system";
  const label = m.kind.startsWith("ai-") ? "AI " + m.kind.slice(3) : m.kind;
  return `<li><time>${dt(m.time)}</time><span class="tag ${kind}">${esc(label)}</span><span>${esc(m.text)}</span></li>`;
}
async function loadMessages() {
  const d = await get("messages");
  $("feed").innerHTML = d.messages.filter((m) => !m.kind.startsWith("ai")).slice(0, 25).map(feedItem).join("") || '<li class="muted">Nothing yet.</li>';
  const ai = d.messages.filter((m) => m.kind.startsWith("ai"));
  $("ai-feed").innerHTML = ai.map(feedItem).join("");
  $("ai-empty").classList.toggle("hidden", ai.length > 0);
  $("ai-empty").textContent = state.last?.brain?.ready
    ? "The AI brain is ON. Its messages appear here when it reviews a buy, suggests strategy ideas (when the Strategy Lab runs) or writes the daily report (00:05 UTC)."
    : "No AI messages yet. Add a free Gemini key in Settings to turn the AI brain on.";
}

/* ----------------------------------------------------------- chart */
async function loadCandles() {
  if (!state.symbol) return;
  $("sym-buttons").querySelectorAll("button").forEach((b) => b.classList.toggle("active", b.dataset.sym === state.symbol));
  const d = await get("candles?symbol=" + encodeURIComponent(state.symbol));
  const c = chart("price-chart"); if (!c || !d.candles) return;
  if (!c.series.k) c.series.k = c.chart.addCandlestickSeries({ upColor: "#22c55e", downColor: "#ef4444", borderVisible: false, wickUpColor: "#22c55e", wickDownColor: "#ef4444" });
  const k = c.series.k;
  k.setData(d.candles);
  const first = d.candles.length ? d.candles[0].time : 0, last = d.candles.length ? d.candles[d.candles.length - 1].time : 0;
  const snap = (t) => Math.min(last, Math.floor(t / 3600) * 3600);
  const markers = [];
  d.trades.forEach((t) => {
    if (t.exit_time < first) return;
    if (t.entry_time >= first) markers.push({ time: snap(t.entry_time), position: "belowBar", color: "#22c55e", shape: "arrowUp", text: "buy" });
    markers.push({ time: snap(t.exit_time), position: "aboveBar", color: t.pnl > 0 ? "#22c55e" : "#ef4444", shape: "arrowDown", text: (t.pnl > 0 ? "+" : "") + t.pnl.toFixed(0) });
  });
  d.open.forEach((p) => { if (p.entry_time >= first) markers.push({ time: snap(p.entry_time), position: "belowBar", color: "#3b82f6", shape: "arrowUp", text: "open" }); });
  markers.sort((a, b) => a.time - b.time);
  k.setMarkers(markers);
  (c.lines || []).forEach((l) => k.removePriceLine(l));
  c.lines = [];
  d.open.forEach((p) => {
    c.lines.push(k.createPriceLine({ price: p.stop, color: "#ef4444", lineStyle: 2, lineWidth: 1, title: "stop" }));
    c.lines.push(k.createPriceLine({ price: p.take, color: "#22c55e", lineStyle: 2, lineWidth: 1, title: "target" }));
  });
}

/* ------------------------------------------------------ strategies */
function metricBox(title, m, honest) {
  if (!m) return "";
  return `<div class="metric ${honest ? "honest" : ""}"><h3>${title}</h3>
    <div><span>Return</span><b class="${cls(m.return_pct)}">${pct(m.return_pct)}</b></div>
    <div><span>Buy &amp; hold</span><span class="${cls(m.buy_hold_return_pct)}">${pct(m.buy_hold_return_pct, 1)}</span></div>
    <div><span>Max drop</span><span>${m.max_dd_pct.toFixed(1)}%</span></div>
    <div><span>Sharpe</span><span>${m.sharpe.toFixed(2)}</span></div>
    <div><span>Trades / wins</span><span>${m.trades} / ${m.win_rate.toFixed(0)}%</span></div></div>`;
}
async function loadStrategies() {
  const d = await get("strategies");
  const st = d.stats || {};
  const ml = st.ml ? Object.entries(st.ml).map(([s, v]) => `${esc(s)} ${(v.auc ?? 0).toFixed(3)}`).join(", ") : "–";
  $("lab-summary").innerHTML = d.updated ? `<div class="card-head"><h2>Last Strategy Lab run</h2><span class="muted small">${dt(d.updated)}</span></div>
     <dl class="status"><dt>Strategies invented &amp; tested</dt><dd>${st.tested ?? "–"}</dd>
     <dt>Ideas from the AI brain</dt><dd>${st.ai_ideas ?? 0}</dd>
     <dt>Best candidates re-tested on unseen data</dt><dd>${st.passed_research ?? "–"}</dd>
     <dt>Passed the unseen-data test</dt><dd>${st.passed_validation ?? "–"}</dd>
     <dt>Chosen for paper trading</dt><dd>${st.chosen ?? 0}</dd>
     <dt>ML model skill (AUC, 0.50 = coin flip)</dt><dd>${ml}</dd>
     <dt>Time taken</dt><dd>${st.seconds ?? "–"} s</dd></dl>`
    : '<div class="empty">The Strategy Lab has not run yet. It starts automatically on first launch.</div>';
  $("strategy-list").innerHTML = (d.active || []).map((a) => {
    const s = a.strategy, live = a.live;
    return `<div class="card strategy"><div class="card-head"><h2>${esc(s.id)}<span class="badge">${esc(s.origin)}</span></h2>
      <span class="muted small">added ${dt(a.added)}</span></div>
      <div class="rules"><p>${esc(a.description.buy)}</p><p class="muted">${esc(a.description.sell)}</p></div>
      <div class="metrics">${metricBox("Research", a.research)}${metricBox("Validation (unseen)", a.validation)}${metricBox("Final test (honest)", a.final, true)}
      <div class="metric"><h3>Live paper trading</h3>${live ? `<div><span>Trades</span><span>${live.trades}</span></div>
        <div><span>Profit</span><b class="${cls(live.pnl)}">${money(live.pnl)}</b></div><div><span>Win rate</span><span>${live.win_rate.toFixed(0)}%</span></div>
        <div><span>Profit factor</span><span>${live.profit_factor.toFixed(2)}</span></div>` : '<div class="muted">no trades yet</div>'}</div></div></div>`;
  }).join("");
}

/* ---------------------------------------------------------- trades */
async function loadTrades() {
  const d = await get("trades");
  const t = d.trades;
  $("trades-empty").classList.toggle("hidden", t.length > 0);
  const wins = t.filter((x) => x.pnl > 0).length, total = t.reduce((a, x) => a + x.pnl, 0);
  $("trade-stats").textContent = t.length ? `${t.length} trades · ${Math.round((100 * wins) / t.length)}% wins · total ${money(total)}` : "";
  $("trades-body").innerHTML = t.map((x) => `<tr><td>${dt(x.exit_time)}</td><td><b>${esc(x.symbol)}</b></td>
    <td title="${esc(x.note)}">${esc(x.strategy_id)}</td><td>${price(x.entry_price)}</td><td>${price(x.exit_price)}</td>
    <td>${esc(x.reason)}</td><td class="${cls(x.pnl)}">${money(x.pnl)} <span class="small">${pct(x.return_pct)}</span></td></tr>`).join("");
}

/* -------------------------------------------------------- backtest */
const COLORS = ["#3b82f6", "#22c55e", "#f59e0b", "#c084fc", "#14b8a6"];
async function loadBacktest() {
  const d = await get("backtest");
  const has = d.strategies && d.strategies.length;
  $("bt-empty").classList.toggle("hidden", !!has);
  const c = chart("bt-chart"); if (!c) return;
  Object.values(c.series).forEach((s) => c.chart.removeSeries(s)); c.series = {};
  if (!has) { $("bt-body").innerHTML = ""; return; }
  const times = d.times.map((t) => Math.floor(t / 1000));
  const line = (vals) => { const seen = new Set(); return vals.map((v, i) => ({ time: times[i], value: v })).filter((p) => !seen.has(p.time) && seen.add(p.time)); };
  c.series.bh = c.chart.addLineSeries({ color: "#8b95a8", lineWidth: 1, lineStyle: 2, title: "buy & hold" });
  c.series.bh.setData(line(d.buy_hold));
  d.strategies.forEach((s, i) => { const x = c.chart.addLineSeries({ color: COLORS[i % COLORS.length], lineWidth: 2, title: s.id }); x.setData(line(s.equity)); c.series[s.id] = x; });
  c.chart.timeScale().fitContent();
  $("bt-body").innerHTML = d.strategies.flatMap((s) => Object.entries(s.per_symbol).map(([sym, p]) => { const m = p.metrics; return `<tr>
    <td>${esc(s.id)}</td><td>${esc(sym)}</td><td>${m.trades}</td><td>${m.win_rate.toFixed(0)}%</td>
    <td class="${cls(m.return_pct)}">${pct(m.return_pct)}</td><td>${m.max_dd_pct.toFixed(1)}%</td><td>${m.sharpe.toFixed(2)}</td>
    <td class="${cls(m.buy_hold_return_pct)}">${pct(m.buy_hold_return_pct, 1)} <span class="muted small">(drop ${m.buy_hold_max_dd_pct.toFixed(0)}%)</span></td></tr>`; })).join("");
}

/* ------------------------------------------------------------- log */
async function loadLog() {
  const d = await get("log");
  const el = $("log");
  const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 30;
  el.textContent = d.lines.map((l) => `${new Date(l.time * 1000).toLocaleTimeString()}  ${l.level.padEnd(7)} ${l.msg}`).join("\n");
  if (atBottom) el.scrollTop = el.scrollHeight;
}

/* --------------------------------------------------------- actions */
$("btn-pause").addEventListener("click", async () => { await post(state.last?.account.paused ? "resume" : "pause"); loadState(); });
$("btn-lab").addEventListener("click", async () => { const r = await post("lab"); toast(r.ok ? "Strategy Lab started — watch the progress bar." : "Another job is already running."); loadState(); });
$("btn-backtest").addEventListener("click", async () => { const r = await post("backtest"); toast(r.ok ? "Backtest started." : "Another job is already running."); loadState(); });
$("btn-reset-kill").addEventListener("click", async () => { if (confirm("Reset the kill switch and allow trading again?")) { await post("reset-kill"); toast("Kill switch reset."); loadState(); } });
$("btn-reset-account").addEventListener("click", async () => {
  if (!confirm("Delete the paper account history and start again with fake money?")) return;
  let r = await post("reset-account");
  if (!r.ok && confirm(r.msg + " Reset anyway (open trades are deleted)?")) r = await post("reset-account", { force: true });
  toast(r.ok ? "Paper account reset." : r.msg || "Not reset."); refreshAll();
});
$("ai-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.target;
  const r = await post("settings", { provider: f.provider.value, gemini_key: f.gemini_key.value, claude_key: f.claude_key.value });
  f.gemini_key.value = f.claude_key.value = "";
  $("ai-result").textContent = r.ok ? (r.brain.ready ? "Saved. AI brain is ON." : "Saved. AI brain is off (no key for this provider).") : "Not saved.";
  loadState();
});
$("btn-test-ai").addEventListener("click", async () => {
  $("ai-result").textContent = "Testing…";
  const r = await post("test-ai");
  $("ai-result").textContent = (r.ok ? "OK: " : "Failed: ") + (r.msg || "");
});

/* --------------------------------------------------------- refresh */
async function refreshTab() {
  try {
    if (state.tab === "overview") await Promise.all([loadEquity(), loadMessages()]);
    else if (state.tab === "chart") await loadCandles();
    else if (state.tab === "strategies") await loadStrategies();
    else if (state.tab === "trades") await loadTrades();
    else if (state.tab === "ai") await loadMessages();
    else if (state.tab === "backtest") await loadBacktest();
    else if (state.tab === "log") await loadLog();
  } catch (e) { console.warn(e); }
}
async function refreshAll() {
  try { await loadState(); } catch (e) { $("conn").textContent = "bot not reachable — is it running?"; return; }
  refreshTab();
}
let ticks = 0;
setInterval(() => {
  if (document.hidden) return;              // no work while you look elsewhere
  ticks++;
  const jobRunning = state.last?.job?.name;
  if (jobRunning || ticks % 3 === 0) loadState().catch(() => ($("conn").textContent = "bot not reachable — is it running?"));
  if (ticks % 6 === 0 || state.tab === "log") refreshTab();
}, 5000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshAll(); });
refreshAll();
