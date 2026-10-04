# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
  from dispute/fraud operations work at Fundbox and Capital One.
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
