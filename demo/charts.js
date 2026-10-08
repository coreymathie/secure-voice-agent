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
  // whole-number steps for counts: 10 -> 0,2,4..10 rather than 0,2.5,5..10
  const n = max <= 5 && Number.isInteger(max) ? max : [4, 5, 2].find((k) => Number.isInteger(max / k)) || 4;
  return Array.from({length: n + 1}, (_, i) => Math.round((max / n) * i * 100) / 100);
}

// A bar whose right end is rounded (4px) and whose left end (the baseline) is square.
function barPath(x, y, w, h, r = 4) {
  const rr = Math.min(r, w, h / 2);
  if (w <= 0) return "";
  return `M${x},${y}h${w - rr}a${rr},${rr} 0 0 1 ${rr},${rr}v${h - 2 * rr}a${rr},${rr} 0 0 1 -${rr},${rr}h-${w - rr}z`;
}

// Shorten a category label with an ellipsis until it fits in the label column; the full text stays in a <title>.
function fitLabel(node, text, max) {
  node.textContent = text;
  if (typeof node.getComputedTextLength !== "function" || node.getComputedTextLength() <= max) return;
  let n = text.length;
  while (n > 1 && node.getComputedTextLength() > max) {
    n -= 1;
    node.textContent = text.slice(0, n).trimEnd() + "…";
  }
  el("title", {}, node).textContent = text;
}

// Widest label in px (0 when the SVG isn't laid out), so a narrow chart can give its labels more room.
function labelWidth(svg, labels) {
  const t = el("text", {x: 0, y: -100}, svg);
  let w = 0;
  for (const l of labels) {
    t.textContent = l;
    w = Math.max(w, typeof t.getComputedTextLength === "function" ? t.getComputedTextLength() : 0);
  }
  t.remove();
  return w;
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
    const totals = rows.map((r) => series.reduce((a, s) => a + (r.values[s.key] || 0), 0));
    const max = niceMax(Math.max(1, ...totals));
    const height = TOP + rows.length * (BAR + GAP) + AXIS;
    const svg = el("svg", {viewBox: `0 0 ${width} ${height}`, width, height, role: "group"}, host);
    const base = Math.min(170, Math.round(width * 0.36));
    const labelW = Math.max(base, Math.min(Math.round(width * 0.5), Math.ceil(labelWidth(svg, rows.map((r) => r.label))) + 12));
    const valueW = 34;
    const plotW = width - labelW - valueW - 8;
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
      fitLabel(name, r.label, labelW - 10);
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
    const max = niceMax(Math.max(1, ...rows.map((r) => r.value)));
    const height = TOP + rows.length * (BAR + GAP) + AXIS;
    const svg = el("svg", {viewBox: `0 0 ${width} ${height}`, width, height, role: "group"}, host);
    const base = Math.min(210, Math.round(width * 0.44));
    const labelW = Math.max(base, Math.min(Math.round(width * 0.56), Math.ceil(labelWidth(svg, rows.map((r) => r.label))) + 12));
    const plotW = width - labelW - 40;
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
      fitLabel(lab, r.label, labelW - 10);
      const node = el("path", {d: barPath(labelW, y, Math.max(2, x(r.value) - labelW), BAR), fill: color}, svg);
      hover(node, r.value.toLocaleString("en-US"), `${r.label} · ${name}`, color);
      const v = el("text", {class: "val", x: x(r.value) + 6, y: y + BAR / 2 + 4}, svg);
      v.textContent = r.value.toLocaleString("en-US");
    });
  });
  container.append(tableView(["", name], rows.map((r) => [r.label, String(r.value)])));
}

/**
 * Vertical stacked columns over time: points [{label, short, values: {key: n}}], series [{key, label, color}].
 * Columns are at most 18px wide with a 2px gap between stacked segments; the x axis labels every Nth point.
 */
export function columns(container, {points, series, unit = ""}) {
  container.append(legend(series));
  const TOP = 8, AXIS = 24, H = 220;
  frame(container, points.length, (host, width) => {
    const totals = points.map((p) => series.reduce((a, s) => a + (p.values[s.key] || 0), 0));
    const max = niceMax(Math.max(1, ...totals));
    const svg = el("svg", {viewBox: `0 0 ${width} ${H}`, width, height: H, role: "group"}, host);
    const left = Math.max(40, Math.ceil(labelWidth(svg, [max.toLocaleString("en-US")])) + 10);
    const plotW = width - left - 6;
    const plotH = H - TOP - AXIS;
    const step = plotW / points.length;
    const colW = Math.max(2, Math.min(18, step - Math.max(1, step * 0.28)));
    const y = (v) => TOP + plotH - (v / max) * plotH;
    for (const t of ticks(max)) {
      el("line", {class: "gridline", x1: left, x2: width - 6, y1: y(t), y2: y(t)}, svg);
      const lab = el("text", {x: left - 6, y: y(t) + 4, "text-anchor": "end"}, svg);
      lab.textContent = t.toLocaleString("en-US");
    }
    el("line", {class: "baseline", x1: left, x2: width - 6, y1: y(0), y2: y(0)}, svg);
    const every = Math.max(1, Math.ceil(points.length / Math.max(2, Math.floor(plotW / 64))));
    points.forEach((p, i) => {
      const x0 = left + i * step + (step - colW) / 2;
      let acc = 0;
      const present = series.filter((s) => (p.values[s.key] || 0) > 0);
      present.forEach((s, j) => {
        const v = p.values[s.key];
        const top = y(acc + v);
        const bottom = y(acc) - (j > 0 ? 2 : 0); // 2px surface gap above the segment below
        acc += v;
        const h = Math.max(1, bottom - top);
        const node = el("rect", {x: x0, y: top, width: colW, height: h, rx: j === present.length - 1 ? Math.min(3, colW / 2) : 0, fill: s.color}, svg);
        hover(node, `${v.toLocaleString("en-US")}${unit}`, `${s.label} · ${p.label}`, s.color);
      });
      if (i % every === 0) {
        const lab = el("text", {x: x0 + colW / 2, y: H - 6, "text-anchor": "middle"}, svg);
        lab.textContent = p.short || p.label;
      }
    });
  });
  container.append(tableView(["", ...series.map((s) => s.label), "Total"], points.map((p) => [p.label, ...series.map((s) => String(p.values[s.key] || 0)), String(totals_(p, series))])));
}

/**
 * A tiny trend line for a KPI tile (decorative: the tile states the value; aria-hidden).
 */
export function sparkline(values, {width = 120, height = 28, color = "var(--accent)"} = {}) {
  if (!values.length) return "";
  const min = Math.min(...values), max = Math.max(...values);
  const span = max - min || 1;
  const pts = values.map((v, i) => [
    (i / Math.max(1, values.length - 1)) * (width - 4) + 2,
    height - 3 - ((v - min) / span) * (height - 6),
  ]);
  const d = pts.map(([x, yy], i) => `${i ? "L" : "M"}${x.toFixed(1)},${yy.toFixed(1)}`).join("");
  const [lx, ly] = pts[pts.length - 1];
  return `<svg class="spark" viewBox="0 0 ${width} ${height}" width="${width}" height="${height}" aria-hidden="true"><path d="${d}" fill="none" stroke="${color}" stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round"/><circle cx="${lx.toFixed(1)}" cy="${ly.toFixed(1)}" r="2.4" fill="${color}"/></svg>`;
}
