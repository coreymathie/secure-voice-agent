// Corey Mathie, 2026
// Two ways to reach the same Console (demo/engine.py), one interface:
//   DemoAdapter  loads the repo's real Python into Pyodide and calls console_api() in the browser.
//   LiveAdapter  calls the local console server (src/console_server.py), which runs the same Console
//                with the real Lambda handler code behind HMAC-signed requests.
// Both expose: mode, label, init(progress) -> info, call(cmd, payload) -> data, can(feature).

export const PYODIDE_URL = "https://cdn.jsdelivr.net/pyodide/v0.26.4/full/";

// Python files fetched from this repo and written into Pyodide's in-memory filesystem.
// Underscore-prefixed files (__init__.py) are created empty instead of fetched.
export const PY_FILES = [
  {"url": "../src/safeguards/pii_redactor.py", "dest": "src/safeguards/pii_redactor.py"},
  {"url": "../src/safeguards/audit_log.py", "dest": "src/safeguards/audit_log.py"},
  {"url": "../src/safeguards/velocity.py", "dest": "src/safeguards/velocity.py"},
  {"url": "../src/safeguards/policy_gate.py", "dest": "src/safeguards/policy_gate.py"},
  {"url": "../src/safeguards/policy_config.py", "dest": "src/safeguards/policy_config.py"},
  {"url": "../src/safeguards/telemetry.py", "dest": "src/safeguards/telemetry.py"},
  {"url": "../src/safeguards/step_up.py", "dest": "src/safeguards/step_up.py"},
  {"url": "../src/agent/tools.py", "dest": "src/agent/tools.py"},
  {"url": "../src/agent/capture.py", "dest": "src/agent/capture.py"},
  {"url": "../src/agent/provider.py", "dest": "src/agent/provider.py"},
  {"url": "../src/handlers/pay_twiml.py", "dest": "src/handlers/pay_twiml.py"},
  {"url": "../src/handlers/call_start.py", "dest": "src/handlers/call_start.py"},
  {"url": "../src/agent/disclosure.py", "dest": "src/agent/disclosure.py"},
  {"url": "../evals/scripted.py", "dest": "evals/scripted.py"},
  {"url": "./engine.py", "dest": "demo/engine.py"}
];
// Repo data the console reads: the reviewed policy, the simulated callers, and the measured eval results.
export const DATA_FILES = {
  policy: "../config/policy.yaml",
  personas: "../evals/personas.yaml",
  evals: "./data/evals.json",
};
const PACKAGES = ["pyyaml", "pydantic"]; // from the Pyodide distribution (policy_config.py validates with them)
const APP = "/home/pyodide/app";

async function fetchText(url) {
  const res = await fetch(url, {cache: "no-cache"});
  if (!res.ok) throw new Error(`could not fetch ${url} (HTTP ${res.status})`);
  return res.text();
}

function loadScript(src) {
  return new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = src;
    s.onload = resolve;
    s.onerror = () => reject(new Error(`Pyodide didn't load from ${new URL(src).host} (blocked or offline?)`));
    document.head.append(s);
  });
}

export class DemoAdapter {
  constructor() {
    this.mode = "demo";
    this.label = "Demo · runs in your browser";
    this.host = "your browser";
    this.files = [];
    this._api = null;
  }

  can(feature) {
    return {server_evals: false, signing_selftest: false, browser_sim: true}[feature] ?? false;
  }

  async init(progress = () => {}) {
    progress("Loading the Python runtime (Pyodide 0.26.4)…");
    if (typeof window.loadPyodide !== "function") await loadScript(PYODIDE_URL + "pyodide.js");
    const pyodide = await window.loadPyodide({indexURL: PYODIDE_URL});
    progress(`Installing ${PACKAGES.join(" and ")} from the Pyodide distribution…`);
    await pyodide.loadPackage(PACKAGES);
    progress(`Fetching ${PY_FILES.length} Python files from the repo…`);
    const texts = await Promise.all(PY_FILES.map((f) => fetchText(f.url)));
    PY_FILES.forEach((f, i) => {
      const dest = `${APP}/${f.dest}`;
      pyodide.FS.mkdirTree(dest.slice(0, dest.lastIndexOf("/")));
      pyodide.FS.writeFile(dest, texts[i]);
      this.files.push({path: f.dest, bytes: texts[i].length});
    });
    for (const pkg of ["src", "src/agent", "src/safeguards", "src/handlers", "evals"]) {
      pyodide.FS.writeFile(`${APP}/${pkg}/__init__.py`, "");
    }
    progress("Reading config/policy.yaml, evals/personas.yaml and the measured eval results…");
    const [policy, personas, evals] = await Promise.all([
      fetchText(DATA_FILES.policy), fetchText(DATA_FILES.personas), fetchText(DATA_FILES.evals).catch(() => ""),
    ]);
    progress("Importing the safeguard modules and validating the policy file…");
    pyodide.runPython(`import sys\nsys.path.insert(0, "${APP}")\nfrom demo import engine as _console_engine`);
    const mod = pyodide.globals.get("_console_engine");
    this._api = mod.console_api;
    this.python = pyodide.runPython("import sys; sys.version.split()[0]");
    const info = JSON.parse(mod.console_init(policy, personas, evals, "demo"));
    return info;
  }

  async call(cmd, payload = {}) {
    if (!this._api) throw new Error("the Python runtime isn't ready yet");
    const out = JSON.parse(this._api(cmd, JSON.stringify(payload)));
    if (!out.ok) throw new Error(out.error);
    return out.data;
  }
}

const TOKEN_KEY = "sva-console-token";

export function getToken() {
  try { return sessionStorage.getItem(TOKEN_KEY) || ""; } catch { return ""; }
}

export function setToken(value) {
  try {
    if (value) sessionStorage.setItem(TOKEN_KEY, value); else sessionStorage.removeItem(TOKEN_KEY);
  } catch { /* storage blocked: the token lives for this page only */ }
}

const CALL_ACTIONS = new Set([
  "say", "tool", "run_pending", "skip_pending", "advance", "payment_mode", "sim_swap", "keypad_result",
  "end_call", "audit_verify", "audit_tamper", "audit_undo",
]);

export class LiveAdapter {
  constructor(meta) {
    this.mode = "live";
    this.meta = meta;
    this.host = meta.host || location.host;
    this.label = `Live · connected to ${this.host}`;
    this.base = meta.api || "/api";
    this.authRequired = !!meta.auth_required;
  }

  can(feature) {
    return {server_evals: true, signing_selftest: true, browser_sim: false}[feature] ?? false;
  }

  route(cmd, p) {
    const id = encodeURIComponent(p.call_id || "");
    if (CALL_ACTIONS.has(cmd)) {
      const {call_id, ...rest} = p;
      return ["POST", `/calls/${id}/${cmd}`, rest];
    }
    switch (cmd) {
      case "info": return ["GET", "/info"];
      case "overview": return ["GET", "/overview"];
      case "calls_list": return ["GET", "/calls"];
      case "call_detail": return ["GET", `/calls/${id}`];
      case "new_call": return ["POST", "/calls", p.opts || {}];
      case "scenarios": return ["GET", "/scenarios"];
      case "run_scenario": return ["POST", `/scenarios/${encodeURIComponent(p.scenario_id)}/run`];
      case "persona_list": return ["GET", "/personas"];
      case "run_persona": return ["POST", `/personas/${encodeURIComponent(p.persona_id)}/run`];
      case "run_personas": return ["POST", "/personas/run", {ids: p.ids || null}];
      case "policy_get": return ["GET", "/policy"];
      case "policy_validate": return ["POST", "/policy/validate", {text: p.text}];
      case "policy_apply": return ["PUT", "/policy", {text: p.text}];
      case "policy_reset": return ["DELETE", "/policy"];
      case "policy_compare": return ["POST", "/policy/compare", {target: p.target}];
      case "evals": return ["GET", "/evals"];
      case "evals_run": return ["POST", "/evals/run"];
      case "settings": return ["GET", "/settings"];
      case "signing_selftest": return ["POST", "/signing/selftest"];
      default: throw new Error(`unknown command ${cmd}`);
    }
  }

  async init(progress = () => {}) {
    progress(`Connecting to the console server at ${this.host}…`);
    return this.call("info");
  }

  async call(cmd, payload = {}) {
    const [method, path, body] = this.route(cmd, payload);
    const headers = {"Accept": "application/json"};
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const token = getToken();
    if (token) headers["Authorization"] = `Bearer ${token}`;
    let res;
    try {
      res = await fetch(this.base + path, {method, headers, body: body !== undefined ? JSON.stringify(body) : undefined});
    } catch (e) {
      throw new Error(`the console server didn't answer (${e.message})`);
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const err = new Error(data.error || `HTTP ${res.status}`);
      err.status = res.status;
      throw err;
    }
    return data;
  }
}

// Mode: ?mode=demo forces the browser engine; otherwise ./api-mode decides. On GitHub Pages that's the static
// demo/api-mode file ({"mode": "demo"}); the console server answers the same path with {"mode": "live", ...}.
export async function detectMode() {
  const forced = new URLSearchParams(location.search).get("mode");
  if (forced === "demo") return {mode: "demo"};
  let meta = {mode: "demo"};
  try {
    const res = await fetch("./api-mode", {cache: "no-store"});
    if (res.ok) meta = JSON.parse(await res.text());
  } catch { /* no server: demo mode */ }
  if (forced === "live" && meta.mode !== "live") {
    return {mode: "error", error: "Live mode was requested (?mode=live) but no console server answered at ./api-mode. Start it with `docker compose up` or `python -m src.console_server`."};
  }
  return meta;
}

export function makeAdapter(meta) {
  return meta.mode === "live" ? new LiveAdapter(meta) : new DemoAdapter();
}
