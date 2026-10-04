# Architecture

## Split deploy

| Component | Runtime | Why |
|---|---|---|
| Twilio voice webhook | Lambda | One short request per call; verify signature, return TwiML |
| Tool handlers (book, pay, ticket, lead) | Lambda | Short, independent, idempotent, scale to zero |
| Pipecat voice process | Fly.io / ECS | Long-lived WebSocket per call; needs a persistent process |

## Call flow

1. Twilio POSTs to `/voice`. The handler verifies `X-Twilio-Signature`, then returns `<Connect><Stream>` TwiML with the caller's number as a custom `<Parameter>`.
2. Twilio opens a WebSocket to `/stream`. `parse_telephony_websocket` reads the start message: stream SID, call SID, and the caller parameter.
3. `bot.build_pipeline` assembles: Twilio serializer → FastAPI WebSocket transport → user context aggregator (Silero VAD) → LLM → transport output → assistant aggregator. Cascaded providers insert STT before and TTS after the LLM.
4. Each tool is registered with `make_handler(name, caller_id, outcomes=...)`, so the safeguard layer is bound to this caller for the whole call and every tool result is recorded for the summary.
5. On hang-up the transport fires `on_client_disconnected` and the pipeline worker is cancelled. With Twilio REST credentials set, ending the pipeline also hangs up the call.
6. `run_bot` then calls `finalize_call`: the context's messages become a redacted transcript, which is summarized with the tool outcomes and sent to the audit log and the `call_summary` Lambda. This runs in a `finally`, so a dropped call is summarized too.

## Safeguard layer order

For every tool call, in this order:

0. **Pin the payment destination**: for `take_payment`, drop any model-supplied phone number and use the caller's.
1. **Velocity**: deny or flag before anything else runs, keyed by the tool-call id so a replay counts once. Denied calls are still audited.
2. **Scrub + audit**: remove government and financial identifiers from the arguments, then write a `tool_call` entry with fully redacted args, PII counts, what was scrubbed, the salted caller hash, and the idempotency key.
3. **Human handoff**: if a `require_human` rule fired, stop and tell the agent to offer a callback.
4. **Execute**: HTTPS to the Lambda handler with the idempotency key.
5. **Classify the result**: a 4xx becomes `rejected` with the handler's reason; an exception or 5xx becomes a generic `error` with details only in the audit log. Both release the velocity slot, because nothing happened.

## Idempotency, precisely

- Key = the LLM's `tool_call_id`, so a replayed tool call carries the same key.
- The Lambda claims the key with `PutItem` + `attribute_not_exists`, which is atomic across concurrent invocations.
- If the handler raises or returns 4xx/5xx, the claim is deleted so a corrected retry can run.
- Successful claims expire after 24 hours via DynamoDB TTL.
- If DynamoDB is unavailable the handler fails open and logs an error, keeping the agent usable. Flip that to fail-closed for payment-only deployments.

## Provider swap

`AGENT_PROVIDER` picks a `VoiceProvider` subclass. Each returns `{"llm"}` (speech-to-speech) or `{"stt", "llm", "tts"}` (cascaded). Nothing else in the pipeline changes.

## Evals

`evals/harness.py` calls `make_handler` with an in-process executor that invokes the Lambda handler functions directly (same request shape and status mapping as the HTTPS executor). The idempotency table is an in-memory stand-in with the same conditional-write behavior, and a fake clock is injected into `VelocityStore`. Outside services are fakes that record what they receive. See the README for the scenario format.

## Known gaps

- Velocity state is per process; multi-instance deployments need Redis.
- No cross-call memory.
- No outbound dialing.
