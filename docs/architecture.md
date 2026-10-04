# Architecture

## Split deploy

| Component | Runtime | Why |
|---|---|---|
| Twilio voice webhook | Lambda | One short request per call; verify signature, return TwiML |
| Tool handlers (book, pay, ticket, lead, update contact) | Lambda | Short, independent, idempotent, scale to zero |
| Step-up verification (send / check a one-time code) | Voice process, calling Twilio Verify | Per-call state (attempts, lockout, verified flag) lives with the call |
| Pipecat voice process | Fly.io / ECS | Long-lived WebSocket per call; needs a persistent process |

## Call flow

1. Twilio POSTs to `/voice`. The handler verifies `X-Twilio-Signature`, then returns TwiML that plays the fixed AI disclosure, asks for recording consent if needed (`<Gather>` → `/consent`), and then `<Connect><Stream>` with the caller's number, the consent result, and the jurisdiction as custom `<Parameter>`s (`src/handlers/call_start.py`). When the stream starts, the voice process audits `call_disclosure` and starts a recording only if consent allows (`src/agent/disclosure.py`).
2. Twilio opens a WebSocket to `/stream`. `parse_telephony_websocket` reads the start message: stream SID, call SID, and the caller parameter.
3. `bot.build_pipeline` assembles: Twilio serializer → FastAPI WebSocket transport → user context aggregator (Silero VAD) → LLM → transport output → assistant aggregator. Cascaded providers insert STT before and TTS after the LLM.
4. Each tool is registered with `make_handler(name, caller_id, executors, outcomes=..., policy=...)`. `send_verification_code` and `verify_caller` run in-process against the call's `StepUpSession`; the others call Lambdas, so the safeguard layer is bound to this caller for the whole call and every tool result is recorded for the summary. One `CallPolicy` per call is shared by all handlers; its transcript source reads the caller's finalized turns from the LLM context when a tool is called. A catch-all handler (`llm.register_function(None, ...)`) sends any tool name the model invents to the same gate, which denies it.
5. With `PAYMENT_MODE=keypad`, a successful `take_payment` redirects the call to Twilio `<Pay>`; the stream ends, the call's state is parked in `src/agent/resume.py`, and when `<Pay>` finishes Twilio opens a new stream (`resume=keypad_payment`) that picks the state back up. See `docs/pci.md`.
6. While the call runs, `watch_call` (`src/agent/call_limits.py`) checks a per-call timer fed by an observer on the pipeline: after a stretch of caller silence the agent asks if they're still there, and at the idle timeout or the maximum duration it says a short goodbye, the audit log gets `call_ended_by_limit`, and an `EndFrame` ends the pipeline.
7. On hang-up the transport fires `on_client_disconnected` and the pipeline worker is cancelled. With Twilio REST credentials set, ending the pipeline also hangs up the call.
8. `run_bot` then calls `finalize_call` (unless the stream ended for a keypad payment): the context's messages become a redacted transcript, which is summarized with the tool outcomes and sent to the audit log and the `call_summary` Lambda. This runs in a `finally`, so a dropped call is summarized too.

## Safeguard layer order

For every tool call, in this order:

0. **Pin the payment destination**: for `take_payment`, drop any model-supplied phone number and use the caller's.
1. **Policy gate** (`src/safeguards/policy_gate.py`): default-deny allow-list with risk tiers; the caller's social-engineering score (handoff for high-tier tools at the threshold); the account-takeover rule (money movement after a contact change on the same call hands off); step-up verification (high-tier tools need a call verified by a one-time code to the phone on file, see `docs/auth.md`; unverified returns `step_up_required`, `policy_step_up` in the audit log); per-call caps on payment links, cumulative USD, and completed actions. A deny or handoff is audited (`policy_denied` / `policy_handoff`) with a reason code and returns guidance the model can act on. Requests it stops never reach velocity, so they don't use up the caller's attempts. Allowed decisions are recorded on the next audit entry (`policy` field).
2. **Velocity**: deny or flag, keyed by the tool-call id so a replay counts once. Denied calls are still audited.
3. **Scrub + audit**: remove government and financial identifiers from the arguments, then write a `tool_call` entry with fully redacted args, PII counts, what was scrubbed, the salted caller hash, and the idempotency key.
4. **Human handoff**: if a `require_human` rule fired, stop and tell the agent to offer a callback.
5. **Execute**: HTTPS to the Lambda handler with the idempotency key.
6. **Classify the result**: a 4xx becomes `rejected` with the handler's reason; an exception or 5xx becomes a generic `error` with details only in the audit log. Both release the velocity slot, because nothing happened. Only results that did something (`booked`, `link_sent`, `created`, ...) count toward the per-call caps.

Each step also emits an OpenTelemetry span event when tracing is on (`docs/observability.md`). Decisions behind this design: `docs/adr/`. Threats and residual risks: `docs/threat-model.md`.

## Idempotency, precisely

- Key = the LLM's `tool_call_id`, so a replayed tool call carries the same key.
- The Lambda claims the key with `PutItem` + `attribute_not_exists`, which is atomic across concurrent invocations.
- If the handler raises or returns 4xx/5xx, the claim is deleted so a corrected retry can run.
- Successful claims expire after 24 hours via DynamoDB TTL.
- If DynamoDB is unavailable the handler fails open and logs an error, keeping the agent usable. Flip that to fail-closed for payment-only deployments.

## Provider swap

`AGENT_PROVIDER` picks a `VoiceProvider` subclass. Each returns `{"llm"}` (speech-to-speech) or `{"stt", "llm", "tts"}` (cascaded). Nothing else in the pipeline changes.

## Evals

`evals/simulate.py` runs scripted multi-turn callers through the same fakes with a deterministic agent that reacts to tool results (`docs/simulation.md`). `evals/harness.py` calls `make_handler` with an in-process executor that invokes the Lambda handler functions directly (same request shape and status mapping as the HTTPS executor). The idempotency table is an in-memory stand-in with the same conditional-write behavior, and a fake clock is injected into `VelocityStore`. Outside services are fakes that record what they receive. See the README for the scenario format.

## Known gaps

- Tool requests are HMAC-signed by the voice process (`TOOL_API_SECRET`) and verified in `src/handlers/_common.py` before any work, so the Lambdas can't be called around the agent's safeguards. Keys are a shared secret, not per-caller identity; IAM/SigV4 or private networking is a further hardening step (ADR 0003).
- Velocity state is per process by default. With more than one voice-process instance, set `VELOCITY_BACKEND=redis` and `REDIS_URL` so every instance counts every request (`src/safeguards/velocity_redis.py`): one MULTI/EXEC transaction per check, callers keyed by salted hash, wall-clock scores (keep instance clocks in sync), and fail closed if Redis is unreachable. Per-call policy state doesn't need sharing: a call lives on one process (except across a keypad payment, see `docs/pci.md`).
- No cross-call memory.
- No outbound dialing.
