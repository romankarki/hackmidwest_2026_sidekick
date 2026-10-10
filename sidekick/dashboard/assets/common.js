/* ===== Shared: theme, top bar, toast, demo store, line chart ===== */

// ---- Theme (applied ASAP to avoid flash) ----
(function initTheme() {
  let t = null;
  try { t = localStorage.getItem("sidekick-theme"); } catch (e) {}
  if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
})();

function currentTheme() {
  const set = document.documentElement.getAttribute("data-theme");
  if (set) return set;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}
function toggleTheme() {
  const next = currentTheme() === "dark" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", next);
  try { localStorage.setItem("sidekick-theme", next); } catch (e) {}
  renderThemeIcon();
  document.dispatchEvent(new CustomEvent("themechange"));
}
const SUN = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>';
const MOON = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>';
function renderThemeIcon() {
  const b = document.getElementById("theme-btn");
  if (b) b.innerHTML = currentTheme() === "dark" ? SUN : MOON;
}

// ---- Top bar ----
function mountTopbar(active) {
  const el = document.getElementById("topbar");
  if (!el) return;
  const links = [
    ["index.html", "Demo flow", "flow"],
    ["shop.html", "ShopLite (app)", "shop"],
    ["chat.html", "Chat (Slack / Teams / DevRev)", "chat"],
    ["dashboard.html", "Agent dashboard", "dash"],
  ];
  el.className = "topbar";
  el.innerHTML = `
    <a class="brand" href="index.html"><span class="brand-mark">S</span>Sidekick <span class="muted" style="font-weight:500">agent</span></a>
    <nav class="topnav">${links.map(([h, l, k]) => `<a href="${h}" class="${k === active ? "active" : ""}">${l}</a>`).join("")}</nav>
    <span class="spacer"></span>
    <button class="icon-btn" id="reset-btn" title="Reset demo state">↺</button>
    <button class="icon-btn" id="theme-btn" title="Toggle light / dark"></button>`;
  renderThemeIcon();
  document.getElementById("theme-btn").onclick = toggleTheme;
  document.getElementById("reset-btn").onclick = () => { Store.reset(); toast("Demo state reset"); setTimeout(() => location.reload(), 500); };
}

// ---- Toast ----
function toast(msg) {
  let w = document.querySelector(".toast-wrap");
  if (!w) { w = document.createElement("div"); w.className = "toast-wrap"; document.body.appendChild(w); }
  const t = document.createElement("div");
  t.className = "toast"; t.textContent = msg; w.appendChild(t);
  setTimeout(() => t.remove(), 3200);
}

// ---- Demo store (localStorage so pages share state; falls back to memory) ----
const Store = (() => {
  const KEY = "sidekick-demo-v1";
  const defaults = () => ({
    tickets: [
      { id: "TKT-1042", title: "Checkout is super slow this morning", source: "ShopLite widget", kind: "bug", status: "open", ts: "07:14" },
      { id: "TKT-1039", title: "Feature request: let me save products to a wishlist", source: "DevRev portal", kind: "feature", status: "open", ts: "Yesterday" },
    ],
    runs: [],
    fixApplied: false,
    previewReady: false,
  });
  let mem = null;
  function load() {
    if (mem) return mem;
    try { mem = JSON.parse(localStorage.getItem(KEY)) || defaults(); } catch (e) { mem = defaults(); }
    return mem;
  }
  function save() { try { localStorage.setItem(KEY, JSON.stringify(mem)); } catch (e) {} }
  return {
    get: () => load(),
    update(fn) { load(); fn(mem); save(); },
    reset() { mem = defaults(); save(); },
  };
})();

function esc(s) { return String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }
function nowTime() { const d = new Date(); return d.toTimeString().slice(0, 5); }

// ---- Line chart (single series, crosshair tooltip, optional threshold + highlighted band) ----
// data: [{label, value}], opts: {unit, threshold, thresholdLabel, band:[i0,i1], height}
function lineChart(container, data, opts = {}) {
  const W = 640, H = opts.height || 220, P = { l: 44, r: 12, t: 12, b: 26 };
  const raw = Math.max(...data.map(d => d.value), opts.threshold || 0) * 1.05;
  const mag = Math.pow(10, Math.floor(Math.log10(raw / 4)));
  const step = [1, 1.5, 2, 2.5, 3, 4, 5, 6, 7.5, 8, 10].map(m => m * mag).find(s => s * 4 >= raw);
  const max = step * 4;
  const x = i => P.l + (i * (W - P.l - P.r)) / (data.length - 1);
  const y = v => H - P.b - (v / max) * (H - P.t - P.b);
  const ticks = 4;
  let grid = "", axis = "";
  for (let k = 0; k <= ticks; k++) {
    const v = (max / ticks) * k, yy = y(v);
    grid += `<line x1="${P.l}" x2="${W - P.r}" y1="${yy}" y2="${yy}"/>`;
    axis += `<text x="${P.l - 8}" y="${yy + 4}" text-anchor="end">${Math.round(v)}</text>`;
  }
  const every = Math.ceil(data.length / 8);
  data.forEach((d, i) => { if (i % every === 0) axis += `<text x="${x(i)}" y="${H - 6}" text-anchor="middle">${d.label}</text>`; });
  const path = data.map((d, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(d.value).toFixed(1)}`).join("");
  const area = `${path}L${x(data.length - 1)},${y(0)}L${x(0)},${y(0)}Z`;
  const band = opts.band ? `<rect class="band" x="${x(opts.band[0])}" y="${P.t}" width="${x(opts.band[1]) - x(opts.band[0])}" height="${H - P.t - P.b}"/>` : "";
  const thr = opts.threshold ? `<line class="thr" x1="${P.l}" x2="${W - P.r}" y1="${y(opts.threshold)}" y2="${y(opts.threshold)}"/><text class="thr-label" x="${W - P.r}" y="${y(opts.threshold) - 5}" text-anchor="end">${opts.thresholdLabel || "SLO"}</text>` : "";
  container.classList.add("chart");
  container.innerHTML = `
    <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(opts.title || "chart")}">
      <g class="grid">${grid}</g>${band}
      <path class="area" d="${area}"/><path class="series" d="${path}"/>${thr}
      <g class="axis">${axis}</g>
      <line class="cross" y1="${P.t}" y2="${H - P.b}" visibility="hidden"/>
      <circle class="marker" r="5" visibility="hidden"/>
      <rect class="hit" x="${P.l}" y="0" width="${W - P.l - P.r}" height="${H}" fill="transparent"/>
    </svg><div class="tip" hidden></div>`;
  const svg = container.querySelector("svg"), cross = svg.querySelector(".cross"), mk = svg.querySelector(".marker"), tip = container.querySelector(".tip");
  svg.querySelector(".hit").addEventListener("mousemove", e => {
    const r = svg.getBoundingClientRect();
    const sx = ((e.clientX - r.left) / r.width) * W;
    const i = Math.max(0, Math.min(data.length - 1, Math.round(((sx - P.l) / (W - P.l - P.r)) * (data.length - 1))));
    const d = data[i];
    cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i)); cross.setAttribute("visibility", "visible");
    mk.setAttribute("cx", x(i)); mk.setAttribute("cy", y(d.value)); mk.setAttribute("visibility", "visible");
    tip.hidden = false;
    tip.style.left = (x(i) / W) * r.width + "px";
    tip.style.top = (y(d.value) / H) * r.height + "px";
    tip.innerHTML = `<span class="muted">${d.label}</span><b>${d.value}${opts.unit || ""}</b>`;
  });
  svg.querySelector(".hit").addEventListener("mouseleave", () => { cross.setAttribute("visibility", "hidden"); mk.setAttribute("visibility", "hidden"); tip.hidden = true; });
}

// p99 checkout latency 06:00 → 09:00 in 10-minute steps, spike at 07:00
const LATENCY_SERIES = (() => {
  const vals = [212, 205, 220, 214, 231, 226, 690, 1840, 2310, 2050, 1420, 610, 260, 240, 228, 219, 224, 210, 215];
  return vals.map((v, i) => {
    const m = 360 + i * 10;
    return { label: `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`, value: v };
  });
})();
