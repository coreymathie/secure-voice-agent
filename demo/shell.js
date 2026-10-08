// Corey Mathie, 2026
// Console shell: grouped side navigation with sub-pages, breadcrumbs, a command palette (Ctrl/Cmd+K or /),
// "g then letter" shortcuts, a shortcuts sheet (?), and a collapsible sidebar. Product-agnostic: the app passes
// its routes and commands in, so the three consoles in this portfolio share one navigation model.

const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => Array.from(root.querySelectorAll(s));
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const isMac = () => /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent || "");

let cfg = null;
let gPending = 0;

/**
 * cfg: {
 *   home: "Cypress Harbor CU",                         first breadcrumb
 *   routes: {overview: {group: "Monitor", label: "Overview", key: "o", subs: {session: "This session"}}, ...},
 *   commands: () => [{label, hint, section, hash | run}],   extra palette entries (actions, recent records)
 *   go: (hash) => void,                                 navigate (re-renders when the hash is unchanged)
 *   storageKey: "sva",                                  prefix for remembered UI state
 * }
 */
export function initShell(config) {
  cfg = config;
  restoreCollapsed();
  $("#nav-collapse")?.addEventListener("click", () => setCollapsed(!document.body.classList.contains("nav-collapsed")));
  $("#search-btn")?.addEventListener("click", openPalette);
  $("#palette")?.addEventListener("click", (e) => { if (e.target.id === "palette") closePalette(); });
  $("#palette-input")?.addEventListener("input", () => paintPalette());
  $("#palette-input")?.addEventListener("keydown", paletteKeys);
  $("#keys")?.addEventListener("click", (e) => { if (e.target.id === "keys" || e.target.closest("[data-close]")) closeKeys(); });
  $$(".kbd-mod").forEach((n) => { n.textContent = isMac() ? "⌘" : "Ctrl"; });
  document.addEventListener("keydown", globalKeys);
  paintKeys();
}

// ---------- active state + breadcrumbs ----------

export function setActive(screen, sub) {
  $$(".sidenav a[data-route]").forEach((a) => {
    const match = a.dataset.route === screen && (a.dataset.sub || "") === (sub || "");
    if (match) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
    // a parent stays visibly "open" while one of its sub-pages is showing
    a.classList.toggle("in-section", a.dataset.route === screen && !a.dataset.sub && !!sub);
  });
}

export function crumbs(screen, sub) {
  const r = cfg?.routes?.[screen];
  if (!r) return "";
  const parts = [`<a href="#/overview">${esc(cfg.home)}</a>`, `<span>${esc(r.group)}</span>`];
  const subLabel = sub && r.subs ? r.subs[sub] : "";
  parts.push(subLabel ? `<a href="#/${screen}">${esc(r.label)}</a>` : `<span aria-current="page">${esc(r.label)}</span>`);
  if (subLabel) parts.push(`<span aria-current="page">${esc(subLabel)}</span>`);
  return `<nav class="crumbs" aria-label="Breadcrumb">${parts.join('<i aria-hidden="true">/</i>')}</nav>`;
}

// ---------- sidebar collapse ----------

function restoreCollapsed() {
  try { if (localStorage.getItem(`${cfg.storageKey}-nav-collapsed`) === "1") setCollapsed(true, false); } catch { /* storage blocked: start expanded */ }
}

function setCollapsed(on, remember = true) {
  document.body.classList.toggle("nav-collapsed", on);
  const b = $("#nav-collapse");
  if (b) {
    b.setAttribute("aria-pressed", String(on));
    b.setAttribute("aria-label", on ? "Expand the sidebar" : "Collapse the sidebar");
    b.title = on ? "Expand the sidebar" : "Collapse the sidebar";
  }
  if (remember) { try { localStorage.setItem(`${cfg.storageKey}-nav-collapsed`, on ? "1" : "0"); } catch { /* ignore */ } }
}

// ---------- command palette ----------

let items = [];
let sel = 0;

function allEntries() {
  const nav = [];
  for (const [route, r] of Object.entries(cfg.routes)) {
    nav.push({section: "Go to", label: r.label, hint: r.group, hash: `#/${route}`, keys: r.key ? `g ${r.key}` : ""});
    for (const [sub, label] of Object.entries(r.subs || {})) nav.push({section: "Go to", label: `${r.label} › ${label}`, hint: r.group, hash: `#/${route}/${sub}`});
  }
  let extra = [];
  try { extra = cfg.commands ? cfg.commands() : []; } catch { extra = []; }
  return [...extra.filter((c) => c.section !== "Recent"), ...nav, ...extra.filter((c) => c.section === "Recent")];
}

function score(entry, q) {
  if (!q) return 1;
  const hay = `${entry.label} ${entry.hint || ""} ${entry.section}`.toLowerCase();
  const words = q.toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.every((w) => hay.includes(w))) return 0;
  return entry.label.toLowerCase().startsWith(words[0]) ? 3 : 2;
}

function paintPalette() {
  const q = $("#palette-input").value.trim();
  items = allEntries().map((e) => ({e, s: score(e, q)})).filter((x) => x.s > 0).sort((a, b) => b.s - a.s).map((x) => x.e).slice(0, 40);
  sel = Math.min(sel, Math.max(0, items.length - 1));
  const list = $("#palette-list");
  if (!items.length) {
    list.innerHTML = `<li class="pal-empty">No matches for “${esc(q)}”</li>`;
    $("#palette-input").removeAttribute("aria-activedescendant");
    return;
  }
  let last = "";
  list.innerHTML = items.map((it, i) => {
    const head = it.section !== last ? `<li class="pal-section" role="presentation">${esc(it.section)}</li>` : "";
    last = it.section;
    return `${head}<li role="option" id="pal-${i}" data-i="${i}" aria-selected="${i === sel}"><span class="pal-label">${esc(it.label)}</span>${it.hint ? `<span class="pal-hint">${esc(it.hint)}</span>` : ""}${it.keys ? `<kbd>${esc(it.keys)}</kbd>` : ""}</li>`;
  }).join("");
  $("#palette-input").setAttribute("aria-activedescendant", `pal-${sel}`);
  $$("#palette-list [role=option]").forEach((li) => {
    li.addEventListener("mousemove", () => { if (sel !== +li.dataset.i) { sel = +li.dataset.i; markSel(); } });
    li.addEventListener("click", () => { sel = +li.dataset.i; runSel(); });
  });
}

function markSel() {
  $$("#palette-list [role=option]").forEach((li) => li.setAttribute("aria-selected", String(+li.dataset.i === sel)));
  $(`#pal-${sel}`)?.scrollIntoView({block: "nearest"});
  $("#palette-input").setAttribute("aria-activedescendant", `pal-${sel}`);
}

function runSel() {
  const it = items[sel];
  if (!it) return;
  closePalette();
  if (it.run) it.run();
  else if (it.hash) cfg.go(it.hash);
}

function paletteKeys(e) {
  if (e.key === "ArrowDown") { e.preventDefault(); sel = (sel + 1) % Math.max(1, items.length); markSel(); }
  else if (e.key === "ArrowUp") { e.preventDefault(); sel = (sel - 1 + items.length) % Math.max(1, items.length); markSel(); }
  else if (e.key === "Enter") { e.preventDefault(); runSel(); }
  else if (e.key === "Escape") { e.preventDefault(); closePalette(); }
}

let lastFocus = null;
export function openPalette() {
  const p = $("#palette");
  if (!p || !p.hidden) return;
  lastFocus = document.activeElement;
  p.hidden = false;
  sel = 0;
  $("#palette-input").value = "";
  paintPalette();
  $("#palette-input").focus();
}

function closePalette() {
  const p = $("#palette");
  if (!p || p.hidden) return;
  p.hidden = true;
  lastFocus?.focus?.();
}

// ---------- shortcuts ----------

function paintKeys() {
  const body = $("#keys-list");
  if (!body || !cfg) return;
  const mod = isMac() ? "⌘" : "Ctrl";
  const rows = [
    [`${mod} K`, "Search and jump anywhere"],
    ["/", "Search and jump anywhere"],
    ["?", "Show these shortcuts"],
    ["Esc", "Close a panel, drawer or dialog"],
    ...Object.values(cfg.routes).filter((r) => r.key).map((r) => [`g ${r.key}`, `Go to ${r.label}`]),
  ];
  body.innerHTML = rows.map(([k, v]) => `<tr><td><kbd>${esc(k)}</kbd></td><td>${esc(v)}</td></tr>`).join("");
}

export function openKeys() { const k = $("#keys"); if (k) { k.hidden = false; $("#keys [data-close]")?.focus(); } }
function closeKeys() { const k = $("#keys"); if (k && !k.hidden) k.hidden = true; }

function typing(t) {
  return t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName));
}

function globalKeys(e) {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
    e.preventDefault();
    if ($("#palette")?.hidden === false) closePalette(); else openPalette();
    return;
  }
  if (e.key === "Escape") { closeKeys(); return; }
  if (typing(e.target) || e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.key === "/") { e.preventDefault(); openPalette(); return; }
  if (e.key === "?") { e.preventDefault(); openKeys(); return; }
  if (e.key === "g") { gPending = Date.now(); return; }
  if (gPending && Date.now() - gPending < 1200) {
    gPending = 0;
    const hit = Object.entries(cfg.routes).find(([, r]) => r.key === e.key.toLowerCase());
    if (hit) { e.preventDefault(); cfg.go(`#/${hit[0]}`); }
  }
}
