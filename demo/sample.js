// Corey Mathie, 2026
// The sample company's data files. They keep fixed dates so they stay reproducible; the console shows
// them moved forward so the newest sample call lands today (or yesterday, before that time of day).

const DAY = 86400000;
let company = null;
let calls = null;
let byId = null;
let offsetDays = 0;

function computeShift(throughIso) {
  const through = new Date(throughIso);
  const now = new Date();
  const target = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const secs = (d) => d.getHours() * 3600 + d.getMinutes() * 60 + d.getSeconds();
  if (secs(now) < secs(through)) target.setDate(target.getDate() - 1);
  const base = new Date(through.getFullYear(), through.getMonth(), through.getDate());
  offsetDays = Math.round((target - base) / DAY);
}

async function getJson(path, what) {
  const r = await fetch(path, {cache: "no-cache"});
  if (!r.ok) throw new Error(`Couldn't load ${what} (HTTP ${r.status})`);
  return r.json();
}

export async function loadCompany() {
  if (company) return company;
  company = await getJson("./data/sample_company.json", "the sample company data");
  computeShift(company.recent_calls?.through || `${company.period.end}T17:00:00`);
  return company;
}

export async function loadCalls() {
  if (calls) return calls;
  await loadCompany();
  const data = await getJson("./data/sample_calls.json", "the sample calls");
  calls = data.calls;
  byId = new Map(calls.map((c, i) => [c.id, i]));
  return calls;
}

export function callIndex(id) {
  return byId && byId.has(id) ? byId.get(id) : -1;
}

/** A sample date ("YYYY-MM-DD") or date-time, moved to the console's "today". */
export function shifted(iso) {
  const d = iso.length === 10 ? new Date(`${iso}T12:00:00`) : new Date(iso);
  d.setDate(d.getDate() + offsetDays);
  return d;
}

export function dayLabel(d) {
  const today = new Date();
  const diff = Math.round((new Date(today.getFullYear(), today.getMonth(), today.getDate()) - new Date(d.getFullYear(), d.getMonth(), d.getDate())) / DAY);
  if (diff === 0) return "Today";
  if (diff === 1) return "Yesterday";
  return d.toLocaleDateString("en-US", {weekday: "short", month: "short", day: "numeric"});
}

export const shortDay = (iso) => shifted(iso).toLocaleDateString("en-US", {month: "short", day: "numeric"});
export const longDay = (iso) => shifted(iso).toLocaleDateString("en-US", {weekday: "short", month: "short", day: "numeric"});
export const clock = (iso) => shifted(iso).toLocaleTimeString("en-US", {hour: "numeric", minute: "2-digit"});
export const yearOf = (iso) => shifted(iso).getFullYear();
