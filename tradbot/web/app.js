/* TradBot dashboard - plain JavaScript, no frameworks (fast on weak PCs).
   Only the visible tab is refreshed, and refreshing pauses when the
   browser tab is hidden. */
"use strict";

const $ = (id) => document.getElementById(id);
const LWC = window.LightweightCharts;
const APP_VERSION = "1.6";   // must match tradbot/__init__.py
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
  $("k-total-usd").innerHTML = money(sm.equity - sm.start_balance)
    + (sm.hold_return_pct != null ? ` · just holding the coins: <span class="${cls(sm.hold_return_pct)}">${pct(sm.hold_return_pct)}</span>` : "");
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
  else if (!sm.active_strategies.length) msg = "Research is running and looking for the first strategies that pass the tests. Trading starts as soon as the first ones go live (usually a few minutes after the first start).";
  banner.textContent = msg;
  banner.className = "banner" + (msg ? (acc.kill_switch || e.last_error ? "" : " info") : " hidden");
  const j = s.job;
  $("job").classList.toggle("hidden", !j.name);
  if (j.name) { $("job-name").textContent = j.name; $("job-msg").textContent = j.msg; $("job-bar").style.width = j.pct + "%"; }
  $("btn-backtest").disabled = !!j.name;
  if (s.version !== APP_VERSION) {
    // the page was updated but the running bot is still the old version
    banner.textContent = "Update not finished: the bot that is running is an OLD version. Close the black TradBot window completely (or restart your PC), then open TradBot.bat again.";
    banner.className = "banner";
    return s;
  }
  try {
    renderLive(s);
    if (state.tab === "live") renderLivePage(s);
  } catch (err) { console.warn(err); }

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

/* ------------------------------------------------------------ live */
function pipelineHTML(c) {
  return [["Strategies tested", (c.tested_total || 0).toLocaleString()], ["Research rounds", c.rounds || 0],
    ["In live market test", c.testing || 0], ["LIVE (trading)", c.live || 0], ["Retired", c.retired || 0]]
    .map(([k, v]) => `<div><span>${k}</span><b>${v}</b></div>`).join("");
}
function renderLive(s) {
  const e = s.engine, r = s.research, c = s.pool;
  const fresh = e.last_scan && Date.now() / 1000 - e.last_scan < s.scan_seconds * 2 + 15;
  $("pulse").className = $("pulse-nav").className = "pulse" + (fresh ? " on" : "");
  $("hb").textContent = fresh ? `Live · scanned ${ago(e.last_scan)}` : (e.last_scan ? `last scan ${ago(e.last_scan)}` : "starting…");
  $("pipeline").innerHTML = pipelineHTML(c);
  $("pipeline2").innerHTML = pipelineHTML(c);
  $("research-state").textContent = r.paused ? "paused" : r.state === "waiting" ? "short break" : r.state;
  $("research-msg").textContent = r.msg || "";
  $("research-bar").style.width = (r.state === "running" ? r.pct : r.state === "waiting" ? 100 : 0) + "%";
  $("btn-lab").textContent = r.paused ? "Resume research" : "Pause research";

  $("radar-empty").classList.toggle("hidden", s.radar.length > 0);
  $("radar-note").textContent = `every ${s.scan_seconds}s · buys are confirmed when the hourly candle closes`;
  $("radar-body").innerHTML = s.radar.map((x) => {
    const meter = x.rules.map((q) => `<i class="${q.ok ? "ok" : ""}" title="${esc(q.rule)}: now ${esc(q.now)}"></i>`).join("");
    const miss = x.rules.find((q) => !q.ok);
    const label = { signal: "BUY signal", holding: "holding", waiting: "waiting" }[x.status];
    return `<tr><td><b>${esc(x.symbol)}</b></td><td>${price(x.price)} <span class="small ${cls(x.change_pct)}">${pct(x.change_pct, 1)}</span></td>
      <td>${esc(x.strategy)}</td><td><span class="meter">${meter}</span>${x.met}/${x.total}</td>
      <td class="wait">${miss ? esc(miss.rule) + " <span class=\"small\">(now " + esc(miss.now) + ")</span>" : "all rules met"}</td>
      <td><span class="st ${x.status}">${label}</span></td></tr>`;
  }).join("");
  $("activity").innerHTML = s.activity.map((a) => `<li><time>${new Date(a.time * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}</time>
     <span class="tag ${esc(a.kind)}">${esc(a.kind)}</span><span>${esc(a.msg)}</span></li>`).join("") || '<li class="muted">starting…</li>';
}

/* ------------------------------------------------------- live page */
const MISTAKE = { gave_back: "was in profit, then reversed", bad_entry: "bad entry timing", stopped: "normal stop-loss",
  slow_loss: "didn't play out in time", exit_rule: "closed by exit rule", news_negative: "bought on negative news" };
let lastSeen = 0;
function recent(s, kinds, secs) {
  const now = s.server_time;
  return s.activity.some((a) => kinds.includes(a.kind) && now - a.time < secs);
}
function renderLivePage(s) {
  const c = s.pool, r = s.research, sm = s.summary, act = s.activity;
  const count = (kinds) => act.filter((a) => kinds.includes(a.kind)).length;
  const best = s.radar.find((x) => x.status !== "holding");
  const st = [
    { icon: "🧪", name: "Research", val: (c.tested_total || 0).toLocaleString(), sub: `round ${r.round} · ${r.paused ? "paused" : r.state}`, lit: r.state === "running" },
    { icon: "🔬", name: "Live market test", val: c.testing || 0, sub: "strategies being proven", lit: recent(s, ["lab"], 60) },
    { icon: "⭐", name: "LIVE strategies", val: c.live || 0, sub: `${c.retired || 0} retired so far`, lit: recent(s, ["lab"], 60) && (c.live || 0) > 0 },
    { icon: "📡", name: "Scanning", val: `${s.symbols.length} coins`, sub: `every ${s.scan_seconds}s · ${ago(s.engine.last_scan)}`, lit: recent(s, ["scan"], s.scan_seconds + 8) },
    { icon: "🎯", name: "Signals", val: count(["signal"]), sub: best ? `closest: ${best.symbol} ${best.met}/${best.total} rules` : "waiting", lit: recent(s, ["signal"], 120) },
    { icon: "🛡️", name: "Filter · Risk · News · AI", val: count(["filter", "risk", "news", "ai"]), sub: `checks before every buy · filter ${s.filter && s.filter.enabled ? "ON" : "OFF"}`, lit: recent(s, ["filter", "risk", "news", "ai"], 120), warn: true },
    { icon: "💱", name: "Trades", val: `${s.positions.length} open`, sub: `${sm.total_trades} finished`, lit: recent(s, ["trade"], 300) || s.positions.length > 0 },
    { icon: "🧠", name: "Lessons", val: sm.total_trades, sub: "learned from every trade", lit: recent(s, ["lesson"], 300) },
  ];
  $("map").innerHTML = st.map((x) => `<div class="station ${x.lit ? (x.warn ? "warn" : "lit") + " flow" : ""}">
      <div class="orb" aria-hidden="true">${x.icon}</div><b>${esc(x.val)}</b><div class="name">${x.name}</div><div class="sub">${esc(x.sub)}</div></div>`).join("");
  $("live-clock").textContent = "now " + new Date(s.server_time * 1000).toLocaleTimeString();

  const newest = act.length ? act[0].time : 0;
  $("timeline").innerHTML = act.map((a) => `<li class="k-${esc(a.kind)} ${lastSeen && a.time > lastSeen ? "new" : ""}">
      <div class="when"><span>${new Date(a.time * 1000).toLocaleTimeString()}</span><span class="tag ${esc(a.kind)}">${esc(a.kind)}</span></div>
      <div class="what">${esc(a.msg)}</div></li>`).join("") || '<li class="k-system"><div class="what muted">starting…</div></li>';
  lastSeen = newest;

  const n = s.news || {}, mood = n.mood || {};
  $("news-note").textContent = n.error ? `news unavailable: ${n.error} (retrying)` : n.updated ? `scored by ${n.source} · ${ago(n.updated)}` : "first check soon";
  $("news-mood").innerHTML = Object.entries(mood).map(([k, v]) => {
    const w = Math.min(50, Math.abs(v.score) * 50), left = v.score < 0 ? 50 - w : 50;
    return `<div class="mood-row" title="${esc(v.reason)}"><span>${esc(k)}</span>
      <div class="mood-bar"><i style="left:${left}%;width:${w}%;background:${v.score < 0 ? "var(--down)" : "var(--up)"}"></i></div>
      <span class="${cls(v.score)}">${v.score > 0 ? "+" : ""}${v.score.toFixed(1)}</span></div>`;
  }).join("") || '<div class="muted small">No news read yet.</div>';
  $("headlines").innerHTML = (n.headlines || []).slice(0, 6).map((h) =>
    `<li>${h.link ? `<a href="${esc(h.link)}" target="_blank" rel="noopener noreferrer">${esc(h.title)}</a>` : esc(h.title)}
      <div class="muted small">${ago(h.time)}${h.coins && h.coins.length ? " · " + esc(h.coins.join(", ")) : ""}</div></li>`).join("");

  const L = s.learning || {}, F = s.filter || {};
  const fline = F.trades ? `Trade filter ${F.enabled ? "ON" : "OFF"}: learned from ${F.trades.toLocaleString()} past trades. On ${F.test_trades} newer trades it never saw: all trades averaged ${pct(F.all_avg_pct)} (${F.all_win_rate}% wins)` + (F.threshold != null ? `, the trades it keeps averaged ${pct(F.kept_avg_pct)} (${F.kept_win_rate}% wins, keeps ${Math.round(F.kept_frac * 100)}%).` : ".") : `Trade filter: ${F.status || "not trained yet"}.`;
  $("insights").innerHTML = [fline, ...(L.insights || [])].map((x) => `<li>${esc(x)}</li>`).join("");
  $("mistakes").innerHTML = Object.entries(L.mistakes || {}).map(([k, v]) => `<span>${esc(MISTAKE[k] || k)} × ${v}</span>`).join("");
}

/* ------------------------------------------------------ strategies */
const fmtTest = (m) => m ? `<span class="${cls(m.return_pct)}">${pct(m.return_pct)}</span> <span class="muted small">· ${m.trades} trades · PF ${(m.profit_factor ?? 0).toFixed(2)}</span>` : "–";
async function loadStrategies() {
  const d = await get("strategies");
  $("pipeline2").innerHTML = pipelineHTML(d.counts);
  const pool = d.pool || [];
  $("pool-empty").classList.toggle("hidden", pool.length > 0);
  $("pool-note").textContent = pool.length ? `${d.counts.live} live · ${d.counts.testing} in live market test (showing ${pool.length})` : "";
  $("pool-body").innerHTML = pool.map((e) => {
    const f = e.forward, live = e.live, st = e.stage === "live" ? (e.probation ? "probation" : "live") : "testing";
    const label = { live: "LIVE", probation: "LIVE · probation", testing: "testing" }[st];
    const fw = f && f.trades ? fmtTest(f) + `<div class="muted small">${f.hours} h of new candles</div>` : `<span class="muted small">no trades yet${f ? " · " + f.hours + " h" : ""}</span>`;
    return `<tr><td><b>${esc(e.strategy.id)}</b><div class="muted small">${esc(e.strategy.origin)}</div></td>
      <td><span class="st ${st}">${label}</span></td>
      <td class="rules-cell">${esc(e.description.buy || "")}</td>
      <td>${fmtTest(e.validation)}<div class="muted small">final test ${pct(e.final?.return_pct)}</div></td>
      <td>${fw}</td>
      <td>${live ? `<span class="${cls(live.pnl)}">${money(live.pnl)}</span> <span class="muted small">${live.trades} trades</span>` : '<span class="muted small">–</span>'}</td></tr>`;
  }).join("");
  const ret = d.retired || [];
  $("retired-empty").classList.toggle("hidden", ret.length > 0);
  $("retired-body").innerHTML = ret.map((e) => `<tr><td>${esc(e.strategy.id)}</td><td>${esc(e.why)}</td><td>${fmtTest(e.forward)}</td><td>${dt(e.retired)}</td></tr>`).join("");
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
    <td>${esc(x.reason)}${x.lesson ? `<span class="lesson">${esc(x.lesson)}</span>` : ""}</td><td class="${cls(x.pnl)}">${money(x.pnl)} <span class="small">${pct(x.return_pct)}</span></td></tr>`).join("");
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
$("btn-lab").addEventListener("click", async () => { const r = await post("research-toggle"); toast(r.paused ? "Research paused." : "Research resumed."); loadState(); });
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
    else if (state.tab === "live") { if (state.last) renderLivePage(state.last); }
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
  if (jobRunning || ticks % 1 === 0) loadState().catch(() => ($("conn").textContent = "bot not reachable — is it running?"));
  if (ticks % 6 === 0 || state.tab === "log") refreshTab();
}, 5000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshAll(); });
refreshAll();
