// Corey Mathie, 2026
// Small inline-SVG charts for the console (no chart library). Specs: bars at most 18px thick, 4px rounded
// data-ends, a 2px surface gap between stacked segments, hairline gridlines, a legend for 2+ series, a value
// at each bar's tip, a hover/focus tooltip on every mark, and a table view so nothing depends on color or hover.
// Categorical slots 1-5 (--series-1..5) were checked with the dataviz palette validator against this surface:
// adjacent CVD ΔE >= 9.1, normal-vision ΔE >= 19.6; three slots are under 3:1 contrast, hence labels + table.

const NS = "http://www.w3.org/2000/svg";

function el(tag, attrs = {}, parent) {
  const node = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "fill" && String(v).startsWith("var(")) node.style.fill = v; // var() only works as a style
    else node.setAttribute(k, String(v));
  }
  if (parent) parent.append(node);
  return node;
}

function niceMax(v) {
  if (v <= 0) return 1;
  const p = 10 ** Math.floor(Math.log10(v));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= v) return m * p;
  return 10 * p;
}

function ticks(max) {
  const n = max <= 4 ? max : 4;
  return Array.from({length: n + 1}, (_, i) => Math.round((max / n) * i * 100) / 100);
}

// A bar whose right end is rounded (4px) and whose left end (the baseline) is square.
function barPath(x, y, w, h, r = 4) {
  const rr = Math.min(r, w, h / 2);
  if (w <= 0) return "";
  return `M${x},${y}h${w - rr}a${rr},${rr} 0 0 1 ${rr},${rr}v${h - 2 * rr}a${rr},${rr} 0 0 1 -${rr},${rr}h-${w - rr}z`;
}

const tip = () => document.getElementById("viz-tip");

function showTip(evt, value, label, color) {
  const t = tip();
  if (!t) return;
  t.replaceChildren();
  const b = document.createElement("b");
  b.textContent = value;
  const row = document.createElement("span");
  if (color) {
    const k = document.createElement("i");
    k.className = "k";
    k.style.background = color;
    row.append(k);
  }
  row.append(document.createTextNode(label));
  t.append(b, row);
  t.hidden = false;
  let x, y;
  if (evt.type === "focus") {
    const r = evt.target.getBoundingClientRect();
    x = r.left + r.width / 2;
    y = r.top;
  } else {
    x = evt.clientX;
    y = evt.clientY;
  }
  const w = t.offsetWidth;
  t.style.left = Math.max(8, Math.min(window.innerWidth - w - 8, x - w / 2)) + "px";
  t.style.top = Math.max(8, y - t.offsetHeight - 12) + "px";
}

function hideTip() {
  const t = tip();
  if (t) t.hidden = true;
}

function hover(node, value, label, color) {
  node.setAttribute("tabindex", "0");
  node.setAttribute("role", "img");
  node.setAttribute("aria-label", `${label}: ${value}`);
  node.classList.add("mark");
  node.addEventListener("pointermove", (e) => showTip(e, value, label, color));
  node.addEventListener("pointerleave", hideTip);
  node.addEventListener("focus", (e) => showTip(e, value, label, color));
  node.addEventListener("blur", hideTip);
}

function legend(series) {
  const ul = document.createElement("ul");
  ul.className = "legend";
  for (const s of series) {
    const li = document.createElement("li");
    const sw = document.createElement("i");
    sw.style.background = s.color;
    li.append(sw, document.createTextNode(s.label));
    ul.append(li);
  }
  return ul;
}

function tableView(head, rows) {
  const d = document.createElement("details");
  d.className = "table-view";
  const s = document.createElement("summary");
  s.textContent = "Show as a table";
  const wrap = document.createElement("div");
  wrap.className = "table-wrap";
  const table = document.createElement("table");
  const thead = table.createTHead().insertRow();
  head.forEach((h, i) => {
    const th = document.createElement("th");
    th.textContent = h;
    if (i > 0) th.className = "num";
    thead.append(th);
  });
  const tb = table.createTBody();
  for (const r of rows) {
    const tr = tb.insertRow();
    r.forEach((c, i) => {
      const td = tr.insertCell();
      td.textContent = c;
      if (i > 0) td.className = "num";
    });
  }
  wrap.append(table);
  d.append(s, wrap);
  return d;
}

function frame(container, rowsCount, draw) {
  const host = document.createElement("div");
  host.className = "viz";
  container.append(host);
  const render = () => {
    const width = Math.max(260, Math.floor(host.clientWidth || container.clientWidth || 600));
    host.replaceChildren();
    draw(host, width);
  };
  render();
  if (typeof ResizeObserver === "function") {
    let last = host.clientWidth;
    const ro = new ResizeObserver(() => {
      if (Math.abs(host.clientWidth - last) > 4) {
        last = host.clientWidth;
        render();
      }
    });
    ro.observe(host);
  }
  return host;
}

/**
 * Horizontal stacked bars: rows [{label, values: {key: n}}], series [{key, label, color}].
 */
export function stackedBars(container, {rows, series, unit = ""}) {
  container.append(legend(series));
  const BAR = 18, GAP = 14, TOP = 6, AXIS = 22;
  frame(container, rows.length, (host, width) => {
    const labelW = Math.min(170, Math.round(width * 0.36));
    const valueW = 34;
    const plotW = width - labelW - valueW - 8;
    const totals = rows.map((r) => series.reduce((a, s) => a + (r.values[s.key] || 0), 0));
    const max = niceMax(Math.max(1, ...totals));
    const height = TOP + rows.length * (BAR + GAP) + AXIS;
    const svg = el("svg", {viewBox: `0 0 ${width} ${height}`, width, height, role: "group"}, host);
    const x = (v) => labelW + (v / max) * plotW;
    for (const t of ticks(max)) {
      el("line", {class: "gridline", x1: x(t), x2: x(t), y1: TOP - 4, y2: height - AXIS + 2}, svg);
      const lab = el("text", {x: x(t), y: height - 6, "text-anchor": "middle"}, svg);
      lab.textContent = t.toLocaleString("en-US");
    }
    el("line", {class: "baseline", x1: labelW, x2: labelW, y1: TOP - 4, y2: height - AXIS + 2}, svg);
    rows.forEach((r, i) => {
      const y = TOP + i * (BAR + GAP);
      const name = el("text", {x: labelW - 8, y: y + BAR / 2 + 4, "text-anchor": "end"}, svg);
      name.textContent = r.label.length > 26 ? r.label.slice(0, 25) + "…" : r.label;
      let acc = 0;
      const present = series.filter((s) => (r.values[s.key] || 0) > 0);
      present.forEach((s, j) => {
        const v = r.values[s.key];
        const x0 = x(acc) + (j > 0 ? 1 : 0);
        const x1 = x(acc + v) - (j < present.length - 1 ? 1 : 0);
        acc += v;
        const w = Math.max(1, x1 - x0);
        const last = j === present.length - 1;
        const node = last
          ? el("path", {d: barPath(x0, y, w, BAR), fill: s.color}, svg)
          : el("rect", {x: x0, y, width: w, height: BAR, fill: s.color}, svg);
        hover(node, `${v.toLocaleString("en-US")}${unit}`, `${s.label} · ${r.label}`, s.color);
      });
      const tot = el("text", {class: "val", x: x(totals[i]) + 6, y: y + BAR / 2 + 4}, svg);
      tot.textContent = totals[i].toLocaleString("en-US");
    });
  });
  container.append(tableView(["", ...series.map((s) => s.label), "Total"], rows.map((r, i) => [r.label, ...series.map((s) => String(r.values[s.key] || 0)), String(totals_(r, series))])));
}

function totals_(r, series) {
  return series.reduce((a, s) => a + (r.values[s.key] || 0), 0);
}

/**
 * Horizontal bars, one series: rows [{label, value}].
 */
export function bars(container, {rows, color = "var(--series-1)", name = "count"}) {
  const BAR = 16, GAP = 12, TOP = 6, AXIS = 22;
  frame(container, rows.length, (host, width) => {
    const labelW = Math.min(210, Math.round(width * 0.44));
    const plotW = width - labelW - 40;
    const max = niceMax(Math.max(1, ...rows.map((r) => r.value)));
    const height = TOP + rows.length * (BAR + GAP) + AXIS;
    const svg = el("svg", {viewBox: `0 0 ${width} ${height}`, width, height, role: "group"}, host);
    const x = (v) => labelW + (v / max) * plotW;
    for (const t of ticks(max)) {
      el("line", {class: "gridline", x1: x(t), x2: x(t), y1: TOP - 4, y2: height - AXIS + 2}, svg);
      const lab = el("text", {x: x(t), y: height - 6, "text-anchor": "middle"}, svg);
      lab.textContent = t.toLocaleString("en-US");
    }
    el("line", {class: "baseline", x1: labelW, x2: labelW, y1: TOP - 4, y2: height - AXIS + 2}, svg);
    rows.forEach((r, i) => {
      const y = TOP + i * (BAR + GAP);
      const lab = el("text", {x: labelW - 8, y: y + BAR / 2 + 4, "text-anchor": "end"}, svg);
      lab.textContent = r.label.length > 30 ? r.label.slice(0, 29) + "…" : r.label;
      const node = el("path", {d: barPath(labelW, y, Math.max(2, x(r.value) - labelW), BAR), fill: color}, svg);
      hover(node, r.value.toLocaleString("en-US"), `${r.label} · ${name}`, color);
      const v = el("text", {class: "val", x: x(r.value) + 6, y: y + BAR / 2 + 4}, svg);
      v.textContent = r.value.toLocaleString("en-US");
    });
  });
  container.append(tableView(["", name], rows.map((r) => [r.label, String(r.value)])));
}
