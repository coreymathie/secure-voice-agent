# Console

The console (`demo/`) is one set of static files that runs in two modes:

| | Demo mode | Live mode |
|---|---|---|
| Where | GitHub Pages: `https://coreymathie.github.io/secure-voice-agent/demo/` (or any static server) | `docker compose up` or `python -m src.console_server` → http://localhost:8090/console/ |
| Python | The repo's real modules, loaded into Pyodide 0.26.4 in your browser | The same modules in the console server process |
| Tool backends | In-process stand-ins for the Lambda handlers (`SimulatedBackends` in `demo/engine.py`): same validation messages, same idempotency answer | The real Lambda handler code in `src/handlers/`, called with HMAC-signed requests that the handlers verify; outside services are the eval fakes from `evals/harness.py` |
| Call start | The real disclosure/consent TwiML builders | The real voice and consent webhook Lambdas, with Twilio-signed requests |
| Evals screen | The committed results in `demo/data/evals.json`; you can re-run the simulated callers in the browser | Same, plus **Re-run evals on the server** (the real harness, against the policy you applied) |
| Network | Pyodide and the repo's own files, nothing after load | None outside the server |
| Keys needed | None | None |

The mode is detected from `./api-mode`: on Pages that is the static file `demo/api-mode` (`{"mode": "demo"}`); the console server answers the same path with `{"mode": "live", ...}`. `?mode=demo` forces demo mode; `?mode=live` fails loudly if no server answers. The header badge shows which one you're in.

Neither mode places or answers a phone call, and neither uses a language model. Caller turns are typed; a scripted agent (`PlaygroundAgent` in `demo/engine.py`, keyword rules) proposes tool calls, and the simulated callers use `ScriptedAgent` from `evals/scripted.py`. Both agents are gullible on purpose, so whatever is stopped is stopped by the deterministic layer. Real calls need Twilio, the voice process, and the Lambda deployment ([`deploy.md`](deploy.md)).

## Screens

- **Overview › Business impact**: 90 days of contact-center activity for Cypress Harbor Credit Union, a fictional credit union, from `demo/data/sample_company.json` (written by `python scripts/generate_sample_company.py`, a seeded generator; CI runs it with `--check`). KPI tiles compare the chosen 7/30/90-day range with the period before it; cost avoided multiplies calls resolved by the agent by the per-call cost assumptions stored in the file and shown on screen. The latest calls link to their call pages. A dismissible banner says the workspace is fictional and that the Evals results are measured. Both modes load the same static file.
- **Overview › Test session**: session totals counted from the calls' audit logs (tool calls allowed, blocked, stepped up, handed off; payments by link vs keypad; intact audit chains), the measured eval results, two charts (decisions by tool; controls that fired), and "what to try" cards. On load the 14 simulated callers are played so the dashboards have data.
- **Calls**: the 320 most recent sample calls from `demo/data/sample_calls.json` (written by the same generator, with `scripts/sample_calls.py`): member, reason, outcome, length, the safeguards on the call, and satisfaction. Search covers names, member numbers, phone numbers and anything said; filters by reason, outcome and flags (fraud stopped, payment taken, verification not completed, card number scrubbed, Spanish, low satisfaction); 25 per page; CSV export of the filtered list. `#/call/<id>` is the call page: transcript with the safeguard events interleaved, a replay that steps through it at 8x with safeguard markers on the timeline (recordings aren't part of the sample), the summary, what the agent did, the safeguards in order, and the member. Every call is generated: the controls named on each one are the ones this repo implements, and its intent mix and containment follow the dashboard's assumptions.
- **Test line**: a test call. Pick the caller's state, the consent mode and key pressed, how members pay, a SIM-swap signal, then talk. Each tool call shows its path through the policy gate, velocity, PII scrubbing, request signing (live), and the handler. The simulated phone on file receives one-time codes; keypad mode shows the `<Pay>` TwiML and lets you play Twilio's result. You can also review and edit each proposed tool call before it runs, or act as the model and call any tool directly. Tabs for the 12 guided scenarios and the 14 simulated callers.
- **Calls › Test calls**: every call placed from the test line, guided scenarios, simulated callers and policy re-runs, with search and filters. The drawer shows the decision timeline (each stage's decision and reason, what the model asked for, what the backend received, what the model was told, measured handler time) and the hash-chained audit log, where you can tamper with an entry and watch `verify_chain()` find it.
- **Policies**: edit `config/policy.yaml`, validate it with `policy_config.parse_policy` (the loader the voice process uses at startup), apply it to new calls, and re-run a scenario or simulated caller under the shipped and the applied policy side by side. Errors show inline by field path; an invalid file is never applied.
- **Evals**: the 31 call evals, the 22 mutation tests, the simulated-caller scorecard, and the pytest totals.
- **Settings**: connection and console token (live), voice providers with the environment variables each one reads (live: set or not, never values), tool endpoints, and request signing with a self-test (live).

## Navigation and views

`demo/shell.js` holds the console shell, shared in design with the other consoles in this portfolio: screens grouped by job (Monitor, Try it, Govern, Configure) with sub-pages listed under their parent, breadcrumbs on every screen, a command palette (Ctrl/Cmd+K or `/`) over screens, sub-pages, actions and member calls, `g` then a letter to jump (`g o` Overview, `g c` Calls, `g p` Test line, `g y` Policies, `g e` Evals, `g s` Settings; `?` lists them), a test-call count badge, and a sidebar that collapses to icons (remembered per browser). Below 900px the sidebar becomes a menu.

- **Business and Technical views.** The business view (the default) reads like the product a contact-center team would use. The technical view adds code paths, environment variable names, hashes, TwiML, raw records and the act-as-the-model tool box (elements marked `tech-only`). The choice is remembered per browser; `?view=technical` or `?view=business` sets it from a link.
- **Startup.** The overview, calls and call pages render straight from the static data files. Pyodide (about 10 MB, from jsDelivr) starts in the background for the interactive screens; if it can't load, those screens explain why and the rest of the console keeps working.
- **Guided tour.** Offered once, never forced. Each step opens the screen it describes.
- **Themes.** Light and dark follow the system; the moon button overrides it per browser.

## Live mode API

All under `/api`, JSON in and out. With `CONSOLE_TOKEN` set, every `/api` route needs `Authorization: Bearer <token>` (or `X-Console-Token`); the static console and `/console/api-mode` stay public. The server binds to 127.0.0.1 unless `CONSOLE_HOST` says otherwise, and compose publishes the port on localhost only.

| Method | Path | |
|---|---|---|
| GET | `/console/api-mode` | mode, host, version, whether a token is required |
| GET | `/api/info`, `/api/overview`, `/api/calls`, `/api/calls/{id}`, `/api/scenarios`, `/api/personas`, `/api/policy`, `/api/evals`, `/api/settings` | read-only listings |
| POST | `/api/calls` | start a test call (setup options in the body) |
| POST | `/api/calls/{id}/{action}` | `say`, `tool`, `run_pending`, `skip_pending`, `advance`, `payment_mode`, `sim_swap`, `keypad_result`, `end_call`, `audit_verify`, `audit_tamper`, `audit_undo` |
| POST | `/api/scenarios/{id}/run`, `/api/personas/{id}/run`, `/api/personas/run` | play a guided scenario or simulated caller(s) |
| POST / PUT / DELETE | `/api/policy/validate`, `/api/policy`, `/api/policy` | validate, apply, reset to the shipped file |
| POST | `/api/policy/compare` | one call under the shipped and the applied policy |
| POST | `/api/evals/run` | the real eval harness and simulated callers, against the applied policy |
| POST | `/api/signing/selftest` | one signed request and four tampered ones (unsigned, altered body, stale, re-keyed) to the real `take_payment` handler, plus a replay |
| GET | `/health` | liveness |

`TOOL_API_SECRET` is generated at startup unless you set one. No endpoint returns it and nothing logs it; Settings shows only whether it is configured, where it came from, and how many signatures the handlers accepted and refused. Applying a policy in the console affects console calls only: the voice process reads `config/policy.yaml` at startup, and a change ships through a pull request where CI runs the evals against it.

## Data and checks

- `demo/data/evals.json` is written by `python scripts/export_console_data.py` from the repo's own eval scripts and test run. CI runs it with `--check` and fails if the committed eval and simulator results are stale.
- `demo/data/sample_company.json` and `demo/data/sample_calls.json` are written by `python scripts/generate_sample_company.py` (fixed seed, stated assumptions); CI runs it with `--check`. `tests/test_sample_company.py` checks that every day's outcomes add up, that the files are labelled fictional, that the calls are reproducible and on the last day, that they use only 555-01xx numbers, example.com emails and no full card numbers, and that their resolved share matches the dashboard within 8 points. The console shows sample dates moved forward so the newest call lands today.
- `python scripts/demo_smoke.py` drives every screen in headless Chromium in demo mode, checks there are no page errors and no horizontal scroll at 390px and 1366px, and that the overview and member calls still render with the Pyodide download blocked; `--live` adds the live-mode pass against the server; `--screenshots DIR` saves desktop and phone screenshots; `--pyodide-dir` serves Pyodide from a local copy for offline runs.
- `tests/test_console.py` and `tests/test_console_server.py` cover the engine and the endpoints, including that the console's simulated callers produce exactly what `python -m evals.simulate` does, and that the signing secret never appears in a response.
