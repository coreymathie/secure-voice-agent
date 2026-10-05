# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.7.0] — 2026-10

The single-page safeguard simulator becomes a product console, and the repo is renamed. 410 tests (up from 380), 31 call evals, 22 mutation tests, 14 simulated callers, and a 91-check browser smoke test across both console modes.

**Renamed:** `voice-agent-starter` → `secure-voice-agent` (README, badges, GitHub Pages URL `https://coreymathie.github.io/secure-voice-agent/demo/`, clone instructions, `pyproject.toml` name, the SAM template description, FastAPI titles). Python packages and import paths are unchanged; the `demo/` URL still works.

### Console

Added
- **The console** (`demo/`): a left-sidebar app with hash routing (`#/overview`, `#/playground`, `#/calls/<id>`, ...), deep links, and browser back. Screens: **Overview** (session totals from the calls' audit logs, measured eval pass rates, charts of decisions by tool and controls that fired, "what to try" cards), **Playground** (a test call with disclosure and consent first, a scripted agent that turns typed caller turns into tool calls, editable proposals, step-up on a simulated phone, keypad `<Pay>` TwiML preview; the 12 guided scenarios; the 14 simulated callers), **Call logs** (search and filters; a drawer with the transcript, each tool call's decision timeline, and audit verify / tamper / undo), **Policies** (edit `config/policy.yaml`, validate with the real loader, apply, re-run a call under the shipped and the applied policy side by side, inline errors, a diff against the shipped file), **Evals** (call evals, mutation tests, simulated callers, pytest totals), **Settings** (voice providers and the environment variables they read, tool endpoints, request signing). A five-step guided tour on the first visit. Split into `index.html`, `app.js`, `adapters.js`, `charts.js` (inline SVG, no chart library), and `styles.css`.
- **Two modes, one codebase.** `DemoAdapter` runs the repo's Python in Pyodide 0.26.4 (now including `policy_config.py` with PyYAML and pydantic from the Pyodide distribution, and `provider.py`); `LiveAdapter` calls the console server. The mode comes from `./api-mode` and shows as a header badge ("Demo · runs in your browser" / "Live · connected to <host>").
- **Console server** (`src/console_server.py`, `python -m src.console_server`): FastAPI app serving `demo/` at `/console` with a JSON API (listings, call actions, scenarios, simulated callers, policy validate / apply / reset / compare, evals re-run, signing self-test). Every tool call goes to the real Lambda handler code as an HMAC-signed request that the handler verifies; the signing secret is generated at startup (or `TOOL_API_SECRET`), never returned or logged. Outside services are the eval fakes. Optional `CONSOLE_TOKEN` for `/api/*`; binds to 127.0.0.1 by default.
- `docker-compose.yml` + `Dockerfile.console` + `requirements-console.txt`: `docker compose up` → http://localhost:8090/console/ with no API keys.
- **Console engine** (`demo/engine.py` `Console`): call records, the playground's scripted agent (`PlaygroundAgent`, keyword rules, gullible on purpose), persona runs, policy editing through `policy_config.parse_policy`, overview aggregates, settings read from the code. `console_api()` is the browser bridge; `dispatch()` whitelists the callable methods.
- `scripts/export_console_data.py` writes `demo/data/evals.json` from the repo's own eval scripts and test run (call evals, simulated callers with transcripts, mutation tests, pytest totals). `--check` fails if the committed results are stale; CI runs it.
- `scripts/demo_smoke.py` rewritten: visits every screen in demo mode and does its key interaction, checks the tour, browser back, no page errors, no horizontal scroll at 390px and 1366px; `--live` runs Overview, Playground (signed requests), Call logs, Evals (server re-run), Settings (signing self-test), and Policies against the console server; `--screenshots DIR` saves desktop and phone screenshots. Skips cleanly without Playwright unless `--require`.
- Tests: `tests/test_console.py` (the console's simulated callers match `python -m evals.simulate` persona by persona; playground flows through step-up, social engineering, scrubbing, default deny, keypad capture; policy validation and apply; overview counts; the JS bridge) and `tests/test_console_server.py` (endpoints, signing on every live tool call, keypad Lambda, the signing self-test, secrets never in a response, token auth, JSON errors).
- `docs/console.md`; `docs/img/console.png`.

Changed
- `ScriptedAgent`, persona parsing, goal checks, the controls-fired summary, and the simulator metrics moved from `evals/simulate.py` to `evals/scripted.py` (no harness imports, so the console can run them in the browser); `simulate.py` re-exports them and behaves the same.
- `evals/harness.py`: the service patches are factored into `patch_services()` so the console server can reuse them with its own signing secret.
- `DemoEngine` takes a loaded policy (tiers, caps, signals, velocity rules, step-up settings from the policy file), a backends object, a customer record, risk signals, a code factory, and a well-formed call SID. `SimulatedBackends` records what each outside service would have received (same names as the eval fakes) and, in keypad mode, pauses the recording only if one is running, as `keypad_payment.py` does; the "Pay by keypad" scenario now starts with a recorded call so the pause is visible.
- CI: the `demo-smoke` job is now `console-smoke` and runs both modes, uploading screenshots; the test job checks `demo/data/evals.json` is current.
- `tests/test_demo_engine.py` reads the browser's file list from `demo/adapters.js` (it moved out of `index.html`).
- `.dockerignore` no longer excludes `evals/` (the console image needs the harness; the voice image still copies only `src/` and `config/`).

Not changed: nothing here places or answers phone calls or uses a language model. Every number on the console is either counted from this session's simulated calls or measured by the repo's eval scripts, and labelled as such.

## [0.6.0] — 2026-10

Phase 2, built in checkpoints; each one passed lint, format, tests, evals, and the browser-demo smoke check before it was committed. 370 tests (up from 144; 10 more run when fakeredis is installed), 31 call-eval scenarios (up from 18), 14 simulated callers.

**Breaking changes in this release:** high-tier tools (`take_payment`, `update_contact`) need step-up verification by default (`POLICY_STEP_UP_MIN_TIER=off` restores 0.5.0 behavior); the voice process refuses to start with an invalid `config/policy.yaml` or `POLICY_FILE`, or with a misconfigured `VELOCITY_BACKEND`; the voice webhook's TwiML now begins with an AI disclosure (`<Say>`) and may ask for recording consent before connecting.

### Step-up verification (checkpoint 1)

Added
- **Step-up verification** (`src/safeguards/step_up.py`, `docs/auth.md`). High-tier tools need a call verified by a one-time code sent to the phone **on file**. Caller ID is used only to look the customer up (`CrmLookup`); the code never goes to the calling number as identity or to a number the caller or model supplies. Pluggable `Verifier` (`TwilioVerifyVerifier`, `SimulatedVerifier`, `UnavailableVerifier`) and `RiskSignalProvider` (SIM-swap / number-port hook that blocks the PSTN and hands off). 3 wrong codes per call lock verification and hand off; 3 sends per call; codes masked in the audit log; reasons such as a SIM-swap signal are audited but never shown to the model.
- New tools: `send_verification_code`, `verify_caller` (low tier, in-process, through `make_handler` like everything else) and `update_contact` (high tier; new `src/handlers/update_contact.py` Lambda and SAM resource). `update_contact` gets `customer_id` from the verified session, never the model.
- Policy gate: `step_up_required` (new status, audit event `policy_step_up`) for tools at or above `POLICY_STEP_UP_MIN_TIER` (default `high`; `off` disables), `step_up_locked` handoff after a lockout, and the **account-takeover sequence rule** (`contact_change_then_payment`): once contact details change on a call, money-moving tools hand off even for a verified caller.
- Velocity rules for `send_verification_code` (3 per 10 min), `verify_caller` (6 per hour), and `update_contact` (a second change within 24 h hands off).
- `make_handler` strips an executor's private `_audit` detail from the result before the model sees it, logs it on the `tool_result` entry, and masks `SECRET_ARGS` (the one-time code) before logging.
- Evals (23 total): `caller-id-is-not-identity`, `step-up-then-pay`, `otp-lockout`, `sim-swap-signal`, `account-takeover-sequence`. The harness gives each caller a `StepUpSession` with an in-memory CRM and a simulated verifier; new scenario fields `start_verified`, `customers`, `risk_signals`, the `$CODE` argument, and the `otp_to` check. Five new mutation tests (step-up skipped, codes sent to caller ID, no lockout, no takeover rule, SIM-swap signal ignored).
- Browser demo: a step-up panel (caller ID vs. phone on file, simulated phone inbox, verify, SIM-swap toggle) and three guided scenarios (verify-then-pay, code guessing, change-the-email-then-pay). `scripts/demo_smoke.py` drives the page in headless Chromium; CI runs it in a separate job.

Changed
- **Breaking:** `take_payment` (and the new `update_contact`) now return `step_up_required` until the caller is verified. Production needs `TWILIO_VERIFY_SERVICE_SID` and a `CrmLookup` implementation; without them nobody can verify and high-tier tools hand off (fail closed). `POLICY_STEP_UP_MIN_TIER=off` restores 0.5.0 behavior. Existing eval scenarios about other controls set `start_verified: true`.
- `tools.EXECUTORS` covers Lambda-backed tools only (`tools.LAMBDA_TOOLS`); step-up executors are bound per call with `tools.step_up_executors(session)`. `bot.py` builds one `StepUpSession` per call.
- CI lints and format-checks the whole repo (`ruff check .`), including `scripts/`.
- Docs: `docs/controls.md` C13 (tool API authentication) marked implemented; it was stale after the 0.5.0 request-signing change.

### PCI keypad capture (checkpoint 2)

Added
- **`PAYMENT_MODE=keypad`** (`docs/pci.md`, opt-in; `link` stays the default). `take_payment` keeps its name, tier, step-up, caps, and velocity rules, but `tools.lambda_executors("keypad")` sends it to a new **`keypad_payment` Lambda**: validate the amount (shared `validate_amount`) and call SID, **pause the call recording** (fail closed if it can't be paused; continue if there is none), then redirect the call to `<Say>` + Twilio **`<Pay>`** TwiML. A new **`twilio_pay_result` Lambda** (the `<Pay>` action URL) verifies Twilio's signature, reads only `Result` and `PaymentConfirmationCode`, resumes the recording, and reconnects the call to the agent with the result code. SAM resources and the `PayConnector` parameter added.
- `src/handlers/pay_twiml.py`: standard-library TwiML builders, checked against the Twilio helper library's `<Pay>` attributes in a test.
- `src/agent/capture.py`: per-call `CaptureState` (transcript-suppressed and recording-paused flags) and a `CaptureGuard` Pipecat processor that always drops keypad tones (`InputDTMFFrame`) and drops caller audio and transcripts while a capture is active.
- `src/agent/resume.py`: the call's context, outcomes, `CallPolicy`, and `StepUpSession` are parked when the stream ends for `<Pay>` and picked up by the stream Twilio opens afterwards, so per-call caps and verification can't be reset by the hop and one summary covers the call. Audit events `keypad_handoff`, `keypad_payment_result`, `resume_state_missing`.
- `make_handler(..., on_result=)` hook; `take_payment` gets `call_sid` from the call, never from the model. `keypad_started` counts toward the per-call payment caps.
- Evals (26 total): `keypad-payment`, `keypad-recording-pause-fails`, `keypad-over-limit`; harness support for `payment_mode`, `recording_active`, `keypad_twiml_has`, and `recording_paused_before_capture`. Mutation test: capture without pausing the recording.
- Browser demo: a payment-mode panel showing the generated `<Pay>` TwiML, the capture flags, dropped caller speech during capture, and the TwiML that returns the call; guided scenario "Pay by keypad".

Changed
- `server.py` passes the `resume` and `pay_result` stream parameters to `run_bot`; `build_pipeline` accepts them. `summary.is_kickoff()` filters the resume message from transcripts. `SUCCESS_STATUSES` adds `updated` and `keypad_paid`.

### AI disclosure and recording consent (checkpoint 3)

Added
- **Fixed AI disclosure on every call** (`src/handlers/call_start.py`): the voice webhook's TwiML plays it with `<Say>` before the agent's stream connects, so it never depends on the model (`AI_DISCLOSURE_TEXT`). The system prompt now also tells the agent to confirm it is an AI if asked and never claim to be a person.
- **Recording consent**: recording is off unless `RECORDING_ENABLED=true`. `RECORDING_CONSENT_MODE=always` (default) asks every caller to press 1; `by_jurisdiction` asks callers whose number is in a configurable all-party list (unknown counts as all-party; `BUSINESS_JURISDICTION` can force asking everyone) and plays a notice to the rest; `off` never records. Silence or any other key means no. New `twilio_consent` Lambda and `/consent` route on the local server; SAM parameters `RecordingEnabled`, `RecordingConsentMode`.
- `src/agent/disclosure.py`: when a stream starts, the voice process re-derives the recording rule from its own configuration, starts a Twilio recording only if allowed, and writes a `call_disclosure` audit entry (disclosure given/missing, consent, jurisdiction, recording started/refused/disabled/failed). Not repeated when a call resumes after a keypad payment.
- Evals (31 total): `ai-disclosure-always`, `consent-declined-no-recording`, `consent-no-input-no-recording`, `consent-granted-recording`, `one-party-state-notice`. They run the real webhook Lambdas with Twilio-signed requests. New scenario block `call_start` and checks `call_start_twiml_has/lacks`, `disclosure_before_stream`, `call_disclosure`. Three mutation tests (recording without consent, silence as consent, missing disclosure).
- Browser demo: a call-start panel (state, consent mode, press 1 / 2 / nothing) showing the TwiML and the recording decision; guided scenario "Disclosure, then no to recording".

Changed
- The voice webhook (Lambda and local server) now returns `<Say>` before `<Connect>`; with recording enabled it may return `<Gather>` instead of connecting directly. Stream parameters add `disclosure`, `consent`, and `jurisdiction`. `run_bot` accepts `stream_params`.
- `docs/controls.md` C17 and the EU AI Act Article 50 section marked implemented.

### Policy as code (checkpoint 4)

Added
- **`config/policy.yaml`**: tool tiers (and what each tool can do, plus `enabled`), per-call caps, risk threshold, handoff tier, signal weights and patterns, step-up settings, and velocity rules in one reviewed file. Defaults identical to 0.5.0/0.6.0 code defaults; `tests/test_policy_config.py` proves parity field by field and decision by decision.
- **`src/safeguards/policy_config.py`**: strict schema (pydantic, unknown keys forbidden) plus cross-checks (regexes compile, velocity rules name declared tools). Errors raise `PolicyConfigError` listing each bad field by path. `python -m src.safeguards.policy_config FILE` validates and prints a summary and SHA-256. `POLICY_FILE` selects the file.
- **Fail closed at startup**: `src/agent/server.py` loads the policy on import, so an invalid file stops the voice process (tested in a subprocess).
- `python -m evals.run --policy FILE` runs every scenario against a proposed policy; the evals always use the policy file, never the machine's `POLICY_*` variables. The scorecard prints the policy's hash. Two mutation tests now edit a copy of the policy file (lockout raised, threshold lowered) instead of patching code.
- Each call's audit log starts with `call_started` carrying the policy hash.
- `docs/policy.md`; CI validates the policy file; the Docker image ships `config/`.

Changed
- `PolicyConfig` carries its own `tool_policies`, `signals`, and `max_risk_score` (defaults: the code's `TOOL_POLICIES`, `SIGNALS`, `MAX_RISK_SCORE`), and `CallPolicy` uses them. `PolicyConfig.from_env()` and `StepUpConfig.from_env()` take a `base` (the loaded file); `POLICY_ALLOWED_TOOLS` narrows the base allow-list. `score_turn`/`score_turns` take optional `signals` and `max_score`. `bot.py` builds one process-wide `VelocityStore` from the policy's rules.
- `requirements.txt` lists `pydantic` and `pyyaml` as runtime dependencies.

### Simulated callers (checkpoint 5)

Added
- **`python -m evals.simulate`** (`evals/simulate.py`, `evals/personas.yaml`, `docs/simulation.md`): 14 scripted multi-turn personas (5 benign, 2 impatient, 4 social engineers, 3 prompt injectors) against the real `make_handler` stack, the policy file, step-up, velocity, scrubbing, the audit log, and the Lambda handler code, with a deterministic, deliberately gullible scripted agent (no LLM). Scorecard: task success, correct refusals, false-positive rate, handoffs, and the controls that fired per persona; also checks `leaks` and the audit chain. Exits non-zero if any persona misses its expectation; `--policy` runs it against a proposed policy. CI runs it and uploads the JSON. Text-level only; the docs say what it doesn't measure and where a real model or synthetic speech would plug in.
- `tests/test_simulate.py`: all personas as expected, metrics, reserved phone numbers and example.com only, CLI exit codes, and five mutation tests (step-up skipped, takeover rule removed, default deny removed, scrubbing removed, over-eager threshold raising the false-positive rate).

Changed
- `evals/harness.py`: the fake-service setup is a reusable `fake_world()` context manager.

### Shared velocity store (checkpoint 6)

Added
- **`RedisVelocityStore`** (`src/safeguards/velocity_redis.py`): the velocity windows in Redis sorted sets, same interface and counting rules as `VelocityStore` (every distinct request counts, replays count once via `ZADD NX`, `release()` removes no-op requests). One MULTI/EXEC transaction per check, so concurrent checks from different instances are serialized. Callers are keyed by salted SHA-256 (`AUDIT_SALT`), scores are wall-clock seconds, and an unreachable Redis denies the request (fail closed; `fail_open=True` to opt out).
- `velocity_store_from_env()`: `VELOCITY_BACKEND=memory|redis`, `REDIS_URL`, `VELOCITY_REDIS_PREFIX`. A misconfigured backend raises at startup (the server checks it on import) instead of silently falling back to memory. `bot.py` uses it with the policy file's rules.
- `tests/test_velocity_redis.py`: the same behavior tests against the in-memory store, an in-memory Redis stand-in (`tests/fake_redis.py`), and fakeredis when installed (skipped otherwise), plus a 400-step randomized comparison with the in-memory store, limits across two instances, eight concurrent checks letting exactly one through, no phone numbers in Redis, and fail-closed behavior through `make_handler`. The full eval suite also runs once with the Redis store.
- `requirements.txt`: `redis` (runtime, for the optional backend) and `fakeredis` (dev).

### Call limits (checkpoint 7)

Added
- **Maximum call duration and idle timeout** (`src/agent/call_limits.py`): a per-call `CallTimer` fed by a Pipecat observer (caller speech; the agent's speaking state, so silence counts from the end of the agent's turn) and a `watch_call` task in `run_bot`. Idle: "are you still there?" at `CALL_IDLE_PROMPT_SECONDS` (15), goodbye at `CALL_IDLE_TIMEOUT_SECONDS` (30). Duration: a wrap-up warning `CALL_DURATION_WARNING_SECONDS` (60) before `CALL_MAX_DURATION_SECONDS` (900), then goodbye. At a limit the audit log gets `call_ended_by_limit` (reason, elapsed seconds), the model is asked to say a short goodbye, and after `CALL_GOODBYE_GRACE_SECONDS` an `EndFrame` ends the pipeline (which hangs up when Twilio credentials are set). 0 turns a limit off. The maximum duration spans a keypad-payment hop. Pipecat's own 300 s idle cancel stays as a backstop.
- Injected instructions start with `[call control]` and are excluded from the transcript, the risk scorer, and the summary.
- `tests/test_call_limits.py`: timer rules, the prompt/warning/goodbye/end sequence with a fake clock, the observer, and a silent call ended through `run_bot` with the audit entry.

## [0.5.0] — 2026-10

Added
- **Policy gate** (`src/safeguards/policy_gate.py`), applied in `make_handler()` before the velocity check. Deterministic, no model in the loop:
  - Default-deny allow-list with risk tiers (`log_lead` low; `create_ticket`, `book_meeting` medium; `take_payment` high). `POLICY_ALLOWED_TOOLS` can narrow it, never extend it. A catch-all Pipecat handler (`make_fallback_handler`) routes tool names the model invents to the gate, which denies them.
  - Social-engineering scoring of the caller's finalized turns (urgency, authority claims, requests to skip checks, payment/contact redirects, secrecy). At `POLICY_RISK_THRESHOLD` (default 4), tools at or above `POLICY_HANDOFF_MIN_TIER` (default high) return `require_human`. `bot.py` feeds it the caller's turns from the LLM context at decision time.
  - Per-call caps on payment links (4), cumulative USD across links (5,000), and completed actions (8), all configurable via `POLICY_*`.
  - Decisions are audited (`policy_denied`, `policy_handoff`, or a `policy` field on the next entry) with a reason code, risk score, and signal names, never the caller's words. Refusals return guidance the model can say without revealing thresholds.
- **Five eval scenarios** for the gate (18 total): social-engineering handoff, a benign urgent caller who must not be blocked, the per-call USD ceiling, a backend tool the agent was never granted, the per-call action ceiling. Steps can carry `caller_said`. Four new mutation tests (gate removed, risk scoring removed, per-call counters frozen, over-eager threshold).
- **OpenTelemetry tracing** (`src/agent/tracing.py`): with `OTEL_EXPORTER_OTLP_ENDPOINT` set, Pipecat's tracing is configured with an OTLP exporter and each call's `PipelineWorker` runs with `enable_tracing=True` and the Twilio CallSid as conversation id. Safeguard decisions become span events (`src/safeguards/telemetry.py`), which no-op without OpenTelemetry.
- `python -m evals.latency`: p50/p95/p99 time-to-first-audio and per-service TTFB from OTLP JSON, Jaeger JSON, or Pipecat logs. No latency numbers are published.
- **Browser demo** (`demo/`): a safeguard simulator that runs the real `make_handler()` and `src/safeguards/` modules under Pyodide, with simulated backends, guided scenarios, a live audit log, and tamper detection. Served by GitHub Pages; `.nojekyll` added.
- Docs: ADRs 0001–0004, `docs/threat-model.md` (STRIDE, OWASP Agentic ASI01–ASI10, OWASP LLM Top 10 2025), `docs/controls.md` (NIST AI RMF, NIST AI 600-1, PCI DSS scope notes, EU AI Act Art. 50 marked not implemented), `docs/observability.md`.

- **Signed tool requests.** The voice process signs every tool and call-summary request with HMAC-SHA256 over timestamp, idempotency key and body (`TOOL_API_SECRET`); `_common.idempotent` verifies before claiming the key and returns `401` otherwise. Fails closed when the secret is missing (`ALLOW_UNSIGNED_TOOL_CALLS=true` for local development). The evals send signed requests. **Breaking:** set `TOOL_API_SECRET` in Secrets Manager and on the voice process before upgrading.

Fixed
- The PAN pattern swallowed the separator after a card number ("Card [REDACTED_PAN]was charged"). It now ends on a digit.
- `take_payment` accepted `"nan"` as an amount (NaN passes both the `<= 0` and the over-limit comparisons). Non-finite amounts are now rejected.
- `make_handler(..., executors={})` fell back to the real HTTPS executors because an empty dict is falsy.

Changed
- `scrub_args()` moved to `pii_redactor.py` (still importable from `tools.py`). `make_handler()` accepts `policy=` and `name=None` (catch-all).
- `requirements.txt` adds `pipecat-ai[tracing]` and the OTLP/HTTP exporter. CI lints `demo/`.
- README restructured around the problem, controls, quality evidence, failure modes, and roadmap. 144 tests (up from 55).

## [0.4.0] — 2026-10

Added
- **Call evals** (`evals/`). 13 call scenarios in YAML run through the real tool stack (safeguarded handlers, velocity limits, PII scrubbing, audit log, Lambda handlers with idempotency) against fakes of Stripe, Twilio SMS, Google Calendar, Zendesk, and the CRM that enforce those services' real rules. A fake clock drives the velocity windows. `python -m evals.run` prints a scorecard; CI writes it to the job summary and uploads JSON results. Mutation tests switch off one safeguard at a time and confirm the evals fail.
- **Post-call summaries** (`src/agent/summary.py`). When a call ends, the transcript is PII-redacted and summarized into intent, outcome, follow-ups, and sentiment by Claude (forced tool call) or OpenAI (strict JSON schema), with every tool outcome attached as fact. A rule-based summary is used without a model or if the model fails. Summaries go to the audit log and to a new `call_summary` Lambda that adds a note to the caller's GoHighLevel contact, idempotent on the call SID. Runs even when a call drops.
- OpenAI Realtime now transcribes the caller as well as the agent, so the context and summaries have both sides of the call.
- The system prompt includes the current date and time in `BUSINESS_TIMEZONE`, so "next Tuesday at 2" resolves to a real date.
- Tool results now include `rejected` (with a reason the agent can say) and `sms_failed`, and the prompt tells the agent how to handle each status.

Fixed (each one is covered by an eval scenario)
- A `customer_phone` supplied by the model overrode the caller's number, so a manipulated agent could text a payment link to someone else. Payment links now always go to the calling number.
- Card numbers and SSNs a caller spoke were redacted in the audit log but still sent to Zendesk, the CRM, and the calendar. Identifiers are now scrubbed from tool arguments before they leave the agent.
- An over-limit payment or a malformed timestamp (HTTP 400 from the handler) told the caller "try again later". The handler's reason is now passed to the agent.
- Failed and rejected requests counted toward velocity limits, so a caller retrying after a Stripe outage or correcting an amount was told "too many requests". Requests that had no effect are now released.
- A replayed tool-call id was counted twice by the velocity limits. Replays are now counted once and reach the idempotency layer, which answers `duplicate`.
- Bookings with a local time and no UTC offset (what models usually send) were rejected by Google Calendar. The business time zone is now attached.
- If the SMS failed after the payment link was created, the handler raised, released its idempotency key, and a retry created a second link. It now returns `sms_failed` and keeps the key.

Changed
- `build_pipeline` returns a `CallSession` (task, transport, context, outcomes). The runner uses Pipecat's `WorkerRunner` (`PipelineRunner` is deprecated).
- `VelocityStore` takes an injectable clock and supports `release()`; `make_handler` accepts injected velocity, audit, and outcome stores.
- 55 tests (up from 29). CI lints `evals/` and runs the evals.

## [0.3.0] — 2026-10

Fixed: the safeguard layer is now actually in the call path, and the agent targets the current Pipecat API.

- **Safeguards wired in.** Every tool is registered through `make_handler(name, caller_id)`, so velocity checks, PII-redacted audit logging, and idempotency run on every real call. (0.2.0 shipped the modules but registered the raw executors.)
- **Pipecat 1.x.** Rewrote `bot.py` and `provider.py` for Pipecat 1.12: `FunctionCallParams` handlers, `ToolsSchema`, `LLMContext` + aggregator pair with Silero VAD, `PipelineWorker`, Settings-based services, `parse_telephony_websocket`.
- **Caller identity.** The caller's number is passed from TwiML as a stream parameter, bound to each tool handler, and stored in the audit log only as a salted hash.
- **Twilio signature validation** on both the Lambda webhook and the local `/voice` endpoint.
- **Auto hang-up** enabled only when Twilio REST credentials are present (Pipecat refuses to start otherwise).
- **Idempotency** now claims keys with a conditional DynamoDB write and releases them on failure, so a failed call no longer blocks a legitimate retry.
- **Payments** text a Stripe Payment Link to the caller with a configurable max amount; the URL isn't returned to the LLM when texted.
- **Lambda packaging:** handlers import correctly as top-level modules; per-function `requirements.txt`; secrets resolved from AWS Secrets Manager in the SAM template; service-account JSON accepted inline.
- **Velocity store** trims old events so memory stays bounded.
- **Deployable image.** Added a `Dockerfile` (non-root user) and moved `fly.toml` to the repo root; the old config pointed Fly at a bare Python image with no app code. The audit log is on a persistent Fly volume.
- **Run command** is `python -m src.agent.server` (the old `python src/agent/server.py` failed on relative imports).
- Tests: 29 passing, covering safeguards, handlers, the webhook, and Pipecat wiring for all three providers. Ruff rules pinned in `pyproject.toml`; CI runs lint, format check, and tests.

## [0.2.0] — 2026-10

Added — the compliance half of a production voice agent.

- `src/safeguards/pii_redactor.py` — pattern-based redaction of SSN, PAN
  (Luhn-validated), DOB, bank routing/account, email, phone, driver's license.
  Returns a count-by-rule so the agent can score fraud risk alongside the
  redaction.
- `src/safeguards/audit_log.py` — hash-chained append-only audit log. Each
  entry carries the SHA-256 of the previous, so tampering becomes detectable.
  `verify_chain()` returns the first bad line number.
- `src/safeguards/velocity.py` — fintech-style velocity checks on
  state-changing tool calls (take_payment, book_meeting, create_ticket,
  log_lead). "Deny / flag / require_human" actions per rule. Pattern applied
  from dispute/fraud operations work in fintech.
- `docs/compliance.md` — HIPAA / SOC2-adjacent deployment notes drawn from
  client production experience.

## [0.1.0] — 2026-10

Initial release.

- Pipecat + Twilio + OpenAI Realtime voice agent
- AWS Lambda handlers for Google Calendar, Stripe, Zendesk, GoHighLevel
- Idempotency via DynamoDB
- Swappable provider: OpenAI Realtime, Anthropic + ElevenLabs, Gemini Live
- AWS SAM template + Fly.io config
- CI with ruff + pytest
