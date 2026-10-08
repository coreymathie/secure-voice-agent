// Corey Mathie, 2026
// Member calls: the call list and the call detail page for the sample credit union's recent calls.
// Static data (demo/data/sample_calls.json), so these screens work before, or without, the Python runtime.
import {callIndex, clock, dayLabel, loadCalls, shifted} from "./sample.js";

const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => Array.from(root.querySelectorAll(s));
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const num = (n) => Number(n || 0).toLocaleString("en-US");
const mss = (s) => `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, "0")}`;
const usd = (n) => "$" + Number(n || 0).toLocaleString("en-US", {minimumFractionDigits: 2, maximumFractionDigits: 2});
const maskPhone = (p) => p.replace(/\) (\d{3})-(\d{2})/, ") •••-••");
const PAGE = 25;

const OUTCOME = {
  resolved: ["ok", "Resolved"],
  transferred: ["warn", "Transferred"],
  abandoned: ["", "Hung up"],
};
const FLAG_FILTERS = [
  ["Fraud stopped", "Fraud stopped"],
  ["Payment", "Payment taken"],
  ["verify-failed", "Verification not completed"],
  ["Card number scrubbed", "Card number scrubbed"],
  ["es", "Spanish"],
  ["low-csat", "Low satisfaction (1–2)"],
];
const KIND_ICON = {pass: "✓", block: "■", handoff: "↗", scrub: "✂"};

const F = {q: "", intent: "", outcome: "", flag: "", sort: "new", page: 0};

function outcomePill(c) {
  const [cls, label] = OUTCOME[c.outcome] || ["", c.outcome];
  return `<span class="pill ${cls}">${label}</span>${c.flags.includes("Fraud stopped") ? ' <span class="pill bad">Fraud stopped</span>' : ""}`;
}

function matches(c) {
  if (F.intent && c.intent !== F.intent) return false;
  if (F.outcome && c.outcome !== F.outcome) return false;
  if (F.flag === "es" && c.language !== "es") return false;
  if (F.flag === "low-csat" && !(c.csat && c.csat <= 2)) return false;
  if (F.flag === "verify-failed" && !/^(failed|held)/.test(c.verification)) return false;
  if (F.flag && !["es", "low-csat", "verify-failed"].includes(F.flag) && !c.flags.includes(F.flag)) return false;
  if (F.q) {
    const q = F.q.toLowerCase();
    const hay = [c.member.name, c.member.member_no, c.member.phone, c.intent, c.summary, c.transfer_reason || "", c.id, ...c.transcript.map((t) => t.text)].join(" ").toLowerCase();
    if (!q.split(/\s+/).every((w) => hay.includes(w))) return false;
  }
  return true;
}

function sorted(list) {
  const out = list.slice();
  if (F.sort === "long") out.sort((a, b) => b.duration_s - a.duration_s);
  else if (F.sort === "old") out.reverse();
  return out;
}

function chipsFor(c) {
  const out = [];
  if (c.verification.startsWith("verified")) out.push('<span class="chip ok">Verified</span>');
  else if (/^(failed|held)/.test(c.verification)) out.push('<span class="chip">Not verified</span>');
  for (const f of c.flags) {
    if (f === "Fraud stopped" || f === "Payment" || f === "Appointment") continue;
    out.push(`<span class="chip neutral">${esc(f)}</span>`);
  }
  if (c.actions.some((a) => a.status === "keypad_paid")) out.push('<span class="chip neutral">Keypad payment</span>');
  return out.length ? `<span class="chips">${out.slice(0, 3).join("")}</span>` : '<span class="muted">—</span>';
}

function stars(n) {
  if (!n) return '<span class="muted">—</span>';
  return `<span class="stars s${n}" aria-label="${n} out of 5">${"★".repeat(n)}<span class="off">${"★".repeat(5 - n)}</span></span>`;
}

function csv(rows) {
  const cols = ["Time", "Member", "Member #", "Reason", "Language", "Outcome", "Transfer reason", "Duration (s)", "Verification", "Flags", "Satisfaction", "Summary"];
  const q = (v) => `"${String(v ?? "").replace(/"/g, '""')}"`;
  const lines = rows.map((c) => [shifted(c.started).toLocaleString("en-US"), c.member.name, `••••${c.member.member_no}`, c.intent, c.language, OUTCOME[c.outcome][1], c.transfer_reason, c.duration_s, c.verification, c.flags.join("; "), c.csat ?? "", c.summary].map(q).join(","));
  return [cols.map(q).join(","), ...lines].join("\n");
}

function download(name, text) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([text], {type: "text/csv"}));
  a.download = name;
  document.body.append(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 0);
}

export function latestCallsHtml(calls, n = 6) {
  return `<div class="table-wrap"><table class="calls"><thead><tr><th>Time</th><th>Member</th><th class="hide-sm">Reason</th><th>Outcome</th></tr></thead><tbody>
    ${calls.slice(0, n).map((c) => `<tr class="click" tabindex="0" data-member-call="${esc(c.id)}"><td class="nw">${esc(clock(c.started))}</td><td>${esc(c.member.name)}<span class="sub show-sm">${esc(c.intent)}</span></td><td class="hide-sm wrap">${esc(c.intent)}</td><td>${outcomePill(c)}</td></tr>`).join("")}
  </tbody></table></div>`;
}

export function wireMemberRows(root, go) {
  $$("tr[data-member-call]", root).forEach((tr) => {
    const open = () => go(`#/call/${encodeURIComponent(tr.dataset.memberCall)}`);
    tr.addEventListener("click", open);
    tr.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } });
  });
}

// ---------- the call list ----------

export async function renderMemberCalls(view, {head, tabs, go}) {
  const lede = "Every call the agent answered: who called, why, what the agent did, and every safeguard it applied along the way.";
  view.innerHTML = head("Calls", lede) + tabs + '<div class="loading"><span class="spinner" aria-hidden="true"></span>Loading calls…</div>';
  const calls = await loadCalls();
  const day = dayLabel(shifted(calls[0].started));
  const resolved = calls.filter((c) => c.outcome === "resolved").length;
  const fraud = calls.filter((c) => c.flags.includes("Fraud stopped")).length;
  const rated = calls.filter((c) => c.csat);
  const avgDur = calls.reduce((a, c) => a + c.duration_s, 0) / calls.length;
  const intents = [...new Set(calls.map((c) => c.intent))].sort();
  view.innerHTML = head("Calls", lede, '<button type="button" id="mc-export">Export CSV</button>') + tabs + `
    <section class="strip" aria-label="${esc(day)} so far">
      <div><span class="k">${esc(day)}, latest calls</span><b>${num(calls.length)}</b></div>
      <div><span class="k">Resolved by the agent</span><b>${Math.round((resolved / calls.length) * 100)}%</b></div>
      <div><span class="k">Average length</span><b>${mss(avgDur)}</b></div>
      <div><span class="k">Fraud attempts stopped</span><b>${fraud}</b></div>
      <div><span class="k">Satisfaction</span><b>${(rated.reduce((a, c) => a + c.csat, 0) / rated.length).toFixed(1)}<small> / 5 · ${rated.length} surveys</small></b></div>
    </section>
    <div class="filters" role="search">
      <div class="grow"><label for="mc-q">Search</label><input id="mc-q" type="search" placeholder="Member, member number, phone, or words said on the call" value="${esc(F.q)}"></div>
      <div><label for="mc-intent">Reason</label><select id="mc-intent"><option value="">All reasons</option>${intents.map((i) => `<option ${F.intent === i ? "selected" : ""}>${esc(i)}</option>`).join("")}</select></div>
      <div><label for="mc-out">Outcome</label><select id="mc-out"><option value="">Any outcome</option>${Object.entries(OUTCOME).map(([k, [, l]]) => `<option value="${k}" ${F.outcome === k ? "selected" : ""}>${l}</option>`).join("")}</select></div>
      <div><label for="mc-flag">Show</label><select id="mc-flag"><option value="">All calls</option>${FLAG_FILTERS.map(([k, l]) => `<option value="${esc(k)}" ${F.flag === k ? "selected" : ""}>${esc(l)}</option>`).join("")}</select></div>
      <div><label for="mc-sort">Sort</label><select id="mc-sort"><option value="new" ${F.sort === "new" ? "selected" : ""}>Newest first</option><option value="old" ${F.sort === "old" ? "selected" : ""}>Oldest first</option><option value="long" ${F.sort === "long" ? "selected" : ""}>Longest first</option></select></div>
    </div>
    <div id="mc-table"></div>
    <p class="hint tech-only">Sample records from <code>demo/data/sample_calls.json</code>, written by <code>scripts/generate_sample_company.py</code>. Calls you place yourself are under <a href="#/calls/test">Test calls</a>.</p>`;
  const paint = () => {
    const rows = sorted(calls.filter(matches));
    const pages = Math.max(1, Math.ceil(rows.length / PAGE));
    F.page = Math.min(F.page, pages - 1);
    const slice = rows.slice(F.page * PAGE, F.page * PAGE + PAGE);
    const box = $("#mc-table", view);
    if (!rows.length) {
      box.innerHTML = '<div class="empty"><b>No calls match</b>Try a different search, or clear the filters. <button type="button" class="sm" id="mc-clear">Clear filters</button></div>';
      $("#mc-clear", box).addEventListener("click", () => { Object.assign(F, {q: "", intent: "", outcome: "", flag: "", page: 0}); renderMemberCalls(view, {head, tabs, go}); });
      return;
    }
    box.innerHTML = `<div class="table-wrap"><table class="calls"><thead><tr><th>Time</th><th>Member</th><th>Reason</th><th>Outcome</th><th class="num hide-sm">Length</th><th class="hide-sm">Safeguards</th><th class="hide-sm">Rating</th></tr></thead><tbody>
      ${slice.map((c) => `<tr class="click" tabindex="0" data-member-call="${esc(c.id)}">
        <td class="nw">${esc(clock(c.started))}</td>
        <td><b class="member">${esc(c.member.name)}</b><span class="sub">•••• ${esc(c.member.member_no)}</span></td>
        <td class="wrap">${esc(c.intent)}${c.language === "es" ? ' <span class="tag">ES</span>' : ""}<span class="sub show-sm">${mss(c.duration_s)}</span></td>
        <td>${outcomePill(c)}${c.transfer_reason ? `<span class="sub">${esc(c.transfer_reason)}</span>` : ""}</td>
        <td class="num hide-sm">${mss(c.duration_s)}</td>
        <td class="hide-sm">${chipsFor(c)}</td>
        <td class="hide-sm">${stars(c.csat)}</td></tr>`).join("")}
      </tbody></table></div>
      <div class="pager"><span class="hint">${num(F.page * PAGE + 1)}–${num(F.page * PAGE + slice.length)} of ${num(rows.length)} calls</span><span class="spacer"></span>
        <button type="button" class="sm" data-page="-1" ${F.page === 0 ? "disabled" : ""}>Previous</button><button type="button" class="sm" data-page="1" ${F.page >= pages - 1 ? "disabled" : ""}>Next</button></div>`;
    wireMemberRows(box, go);
    $$("[data-page]", box).forEach((b) => b.addEventListener("click", () => { F.page += Number(b.dataset.page); paint(); $("#mc-table", view).scrollIntoView({block: "nearest"}); }));
  };
  const on = (id, key, ev = "change") => $(id, view).addEventListener(ev, (e) => { F[key] = e.target.value; F.page = 0; paint(); });
  on("#mc-q", "q", "input");
  on("#mc-intent", "intent");
  on("#mc-out", "outcome");
  on("#mc-flag", "flag");
  on("#mc-sort", "sort");
  $("#mc-export", view).addEventListener("click", () => download("cypress-harbor-calls-sample.csv", csv(sorted(calls.filter(matches)))));
  paint();
}

// ---------- the call detail page ----------

let replay = null;

function stopReplay() {
  if (replay) cancelAnimationFrame(replay.raf);
  replay = null;
}

function transcriptItems(c) {
  const items = c.transcript.map((t) => ({...t, type: "turn"}));
  for (const g of c.safeguards) {
    if (["AI disclosure", "Audit log"].includes(g.control)) continue;
    items.push({t: g.t, type: "guard", g});
  }
  return items.sort((a, b) => a.t - b.t || (a.type === "guard") - (b.type === "guard"));
}

function itemHtml(it, i) {
  if (it.type === "guard") {
    const g = it.g;
    return `<li class="ti guard k-${esc(g.kind)}" data-t="${it.t}"><span class="ts">${mss(it.t)}</span><div><span class="gi" aria-hidden="true">${KIND_ICON[g.kind] || "•"}</span><b>${esc(g.control)}</b> · ${esc(g.result)}</div></li>`;
  }
  const who = {agent: "Harbor (AI agent)", member: "Member", system: "System"}[it.who] || it.who;
  return `<li class="ti ${esc(it.who)}" data-t="${it.t}" data-i="${i}"><span class="ts">${mss(it.t)}</span><div class="bubble"><span class="who">${esc(who)}</span>${esc(it.text)}</div></li>`;
}

function trackHtml(c) {
  const total = Math.max(1, c.duration_s);
  const turns = c.transcript;
  const segs = turns.map((t, i) => {
    const end = i + 1 < turns.length ? turns[i + 1].t : total;
    const w = Math.max(0.6, ((Math.min(end, t.t + Math.max(2, t.text.length / 8)) - t.t) / total) * 100);
    return `<i class="seg-${esc(t.who)}" style="left:${((t.t / total) * 100).toFixed(2)}%;width:${w.toFixed(2)}%"></i>`;
  }).join("");
  const marks = c.safeguards.filter((g) => g.kind !== "pass").map((g) => `<b class="mk k-${esc(g.kind)}" style="left:${((g.t / total) * 100).toFixed(2)}%" title="${esc(g.control)}: ${esc(g.result)}"></b>`).join("");
  return `<div class="track" id="track" role="slider" tabindex="0" aria-label="Call position" aria-valuemin="0" aria-valuemax="${total}" aria-valuenow="0">${segs}${marks}<span class="cursor" id="cursor"></span></div>`;
}

export async function renderMemberCall(view, id, {go, crumbsHome}) {
  stopReplay();
  view.innerHTML = '<div class="loading"><span class="spinner" aria-hidden="true"></span>Loading the call…</div>';
  const calls = await loadCalls();
  const idx = callIndex(id);
  if (idx < 0) {
    view.innerHTML = `<nav class="crumbs" aria-label="Breadcrumb"><a href="#/overview">${esc(crumbsHome)}</a><i aria-hidden="true">/</i><a href="#/calls">Calls</a></nav><div class="empty"><b>We couldn't find that call</b>It may be older than the calls kept in this sample. <a href="#/calls">Back to all calls</a></div>`;
    return;
  }
  const c = calls[idx];
  const newer = calls[idx - 1];
  const older = calls[idx + 1];
  const when = shifted(c.started);
  const items = transcriptItems(c);
  const sentiment = {positive: ["ok", "Positive"], neutral: ["", "Neutral"], negative: ["bad", "Negative"]}[c.sentiment];
  view.innerHTML = `
    <nav class="crumbs" aria-label="Breadcrumb"><a href="#/overview">${esc(crumbsHome)}</a><i aria-hidden="true">/</i><span>Monitor</span><i aria-hidden="true">/</i><a href="#/calls">Calls</a><i aria-hidden="true">/</i><span aria-current="page">${esc(c.member.name)}</span></nav>
    <div class="page-head call-head">
      <div><h1>${esc(c.member.name)} <span class="h-sub">${esc(c.intent)}</span></h1>
        <p class="lede">${outcomePill(c)} <span class="meta-line">${esc(dayLabel(when))} at ${esc(clock(c.started))} · ${mss(c.duration_s)} · ${esc(maskPhone(c.member.phone))} · ${c.language === "es" ? "Spanish" : "English"}</span></p></div>
      <div class="row"><button type="button" class="sm" data-nav="${esc(newer?.id || "")}" ${newer ? "" : "disabled"}>← Newer</button><button type="button" class="sm" data-nav="${esc(older?.id || "")}" ${older ? "" : "disabled"}>Older →</button></div>
    </div>
    <section class="card replay" aria-labelledby="rp-h">
      <div class="replay-head"><h2 id="rp-h" class="sr">Replay</h2><button type="button" class="primary sm" id="rp-play" aria-pressed="false">▶ Replay call</button><span class="rp-time mono" id="rp-time">0:00 / ${mss(c.duration_s)}</span>
        <ul class="legend"><li><i style="background:var(--accent)"></i>Agent</li><li><i style="background:var(--indigo)"></i>Member</li><li><i style="background:var(--line-2)"></i>Phone system</li><li><i class="lg-mk"></i>Safeguard stepped in</li></ul></div>
      ${trackHtml(c)}
      <p class="hint">Recordings aren't included in this sample, so the replay steps through the transcript.</p>
    </section>
    <div class="call-grid">
      <section class="card transcript-card" aria-labelledby="tr-h"><div class="card-head"><h2 id="tr-h">Transcript</h2><span class="hint">${c.transcript.filter((t) => t.who !== "system").length} turns · identifiers scrubbed before storage</span></div>
        <ol class="transcript" id="transcript">${items.map(itemHtml).join("")}</ol></section>
      <div class="stack">
        <section class="card" aria-labelledby="sum-h"><div class="card-head"><h2 id="sum-h">Summary</h2><span class="tag">AI summary</span></div>
          <p class="summary">${esc(c.summary)}</p>
          <dl class="facts">
            <div><dt>Reason</dt><dd>${esc(c.intent)}</dd></div>
            <div><dt>Outcome</dt><dd>${esc(OUTCOME[c.outcome][1])}${c.transfer_reason ? `: ${esc(c.transfer_reason.toLowerCase())}` : ""}</dd></div>
            <div><dt>Identity</dt><dd>${esc(c.verification.charAt(0).toUpperCase() + c.verification.slice(1))}</dd></div>
            <div><dt>Sentiment</dt><dd><span class="pill ${sentiment[0]}">${sentiment[1]}</span></dd></div>
            <div><dt>Satisfaction</dt><dd>${c.csat ? stars(c.csat) : '<span class="muted">No survey</span>'}</dd></div>
            <div><dt>Queue</dt><dd>${esc(c.queue)}</dd></div>
          </dl>
          ${c.qa_tags.length ? `<div class="chips qa">${c.qa_tags.map((t) => `<span class="chip neutral">${esc(t)}</span>`).join("")}</div>` : ""}
        </section>
        <section class="card" aria-labelledby="act-h"><div class="card-head"><h2 id="act-h">What the agent did</h2></div>
          ${c.actions.length ? `<ul class="acts">${c.actions.map((a) => `<li><span class="pill ok">${esc({link_sent: "Link sent", keypad_paid: "Paid", created: "Case opened", booked: "Booked", logged: "Callback set", updated: "Updated"}[a.status] || a.status)}</span><div><b>${esc(a.detail)}</b>${a.amount_usd ? `<span class="hint"> ${usd(a.amount_usd)}</span>` : ""}${a.ticket ? `<span class="hint"> · case ${esc(a.ticket)}</span>` : ""}<span class="sub tech-only mono">${esc(a.tool)} → ${esc(a.status)}</span></div></li>`).join("")}</ul>` : '<p class="hint">No account changes or payments on this call.</p>'}
        </section>
        <section class="card" aria-labelledby="sg-h"><div class="card-head"><h2 id="sg-h">Safeguards</h2><span class="hint">${c.safeguards.filter((g) => g.kind !== "pass").length ? `${c.safeguards.filter((g) => g.kind !== "pass").length} stepped in` : "all checks passed"}</span></div>
          <ol class="guards">${c.safeguards.map((g) => `<li class="k-${esc(g.kind)}"><span class="gi" aria-hidden="true">${KIND_ICON[g.kind] || "•"}</span><div><b>${esc(g.control)}</b><span class="hint">${esc(g.result)}</span></div><span class="ts mono">${mss(g.t)}</span></li>`).join("")}</ol>
        </section>
        <section class="card" aria-labelledby="mem-h"><div class="card-head"><h2 id="mem-h">Member</h2><span class="tag sample">fictional</span></div>
          <dl class="facts">
            <div><dt>Member number</dt><dd>•••• ${esc(c.member.member_no)}</dd></div>
            <div><dt>Member since</dt><dd>${esc(c.member.since)}</dd></div>
            <div><dt>Membership</dt><dd>${esc(c.member.segment)}</dd></div>
            <div><dt>Calls today</dt><dd>${calls.filter((x) => x.member.member_no === c.member.member_no && x.member.name === c.member.name).length}</dd></div>
          </dl>
        </section>
        <details class="card tech-only"><summary><b>Record</b> <span class="hint">risk score ${esc(c.risk_score)}${c.risk_signals.length ? ` · ${esc(c.risk_signals.join(", "))}` : ""} · ${esc(c.id)}</span></summary><pre>${esc(JSON.stringify({...c, transcript: `${c.transcript.length} turns`}, null, 2))}</pre></details>
      </div>
    </div>`;
  $$("[data-nav]", view).forEach((b) => b.addEventListener("click", () => b.dataset.nav && go(`#/call/${encodeURIComponent(b.dataset.nav)}`)));
  wireReplay(view, c);
}

function wireReplay(view, c) {
  const total = Math.max(1, c.duration_s);
  const play = $("#rp-play", view);
  const cursor = $("#cursor", view);
  const track = $("#track", view);
  const list = $("#transcript", view);
  const lis = $$(".ti", list);
  const SPEED = 8;
  let pos = 0;
  const show = (p, scroll = true) => {
    pos = Math.max(0, Math.min(total, p));
    cursor.style.left = `${(pos / total) * 100}%`;
    track.setAttribute("aria-valuenow", String(Math.round(pos)));
    $("#rp-time", view).textContent = `${mss(pos)} / ${mss(total)}`;
    let cur = null;
    for (const li of lis) { if (Number(li.dataset.t) <= pos) cur = li; }
    lis.forEach((li) => { li.classList.toggle("past", Number(li.dataset.t) <= pos); li.classList.toggle("current", li === cur); });
    if (cur && scroll) {
      const top = cur.offsetTop - list.offsetTop;
      if (top < list.scrollTop || top > list.scrollTop + list.clientHeight - 60) list.scrollTo({top: Math.max(0, top - 40), behavior: "smooth"});
    }
  };
  const tick = (now) => {
    if (!replay) return;
    show(replay.from + ((now - replay.start) / 1000) * SPEED);
    if (pos >= total) { stop(); return; }
    replay.raf = requestAnimationFrame(tick);
  };
  const stop = () => { stopReplay(); list.classList.remove("playing"); play.textContent = "▶ Replay call"; play.setAttribute("aria-pressed", "false"); };
  play.addEventListener("click", () => {
    if (replay) { stop(); return; }
    if (pos >= total) show(0);
    replay = {start: performance.now(), from: pos, raf: 0};
    list.classList.add("playing");
    play.textContent = "❚❚ Pause";
    play.setAttribute("aria-pressed", "true");
    replay.raf = requestAnimationFrame(tick);
  });
  const seek = (e) => { const r = track.getBoundingClientRect(); show(((e.clientX - r.left) / r.width) * total); if (replay) { replay.start = performance.now(); replay.from = pos; } };
  track.addEventListener("click", seek);
  track.addEventListener("keydown", (e) => {
    if (e.key === "ArrowRight") { e.preventDefault(); show(pos + 5); } else if (e.key === "ArrowLeft") { e.preventDefault(); show(pos - 5); }
  });
  lis.forEach((li) => li.addEventListener("click", () => show(Number(li.dataset.t), false)));
  show(0, false);
}

export function leaveMemberCall() {
  stopReplay();
}
