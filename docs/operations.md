# Operations

This document describes how the system behaves when something goes wrong, which failures fail closed and which fail open, and where the operational detail for observability and deployment lives. It is the reference for an operations or risk reviewer asking "what does the caller hear, and what does the system record, when this component fails?"

## Operating posture

- **Fail closed** wherever money, identity or regulated data is at stake: an invalid policy file stops the voice process, missing verification infrastructure hands high-tier tools to a person, an unpausable recording blocks card capture, an unreachable shared velocity store denies, and tool Lambdas refuse every request when the signing secret is missing.
- **Fail open, by documented exception:** the idempotency store. If DynamoDB is unavailable the handler logs an error and continues, keeping the agent usable. Payment-only deployments should flip this to fail closed ([`architecture.md`](architecture.md#idempotency-precisely)).
- **Escalate rather than act:** policy, velocity and step-up decisions that are not a clean allow return `require_human` with guidance the model can say. Handoffs are audited; routing them to a review queue is not implemented.

## Failure modes

| What happens | What the caller hears / what the system does |
|---|---|
| Caller presses 2 or nothing at the recording-consent prompt | The call continues unrecorded; `call_disclosure` audits `refused_no_consent`. |
| A call reaches the agent without the webhook's TwiML | Audited as `disclosure: missing`; never recorded. |
| Caller asks to pay or change contact details, not yet verified | `step_up_required`; the agent offers a code to the phone on file. |
| Caller ID is spoofed | The code goes to the real number on file, so the caller can't read it back; three guesses lock the call and hand off (`step_up_locked`). |
| No CRM lookup or Twilio Verify configured | Nobody can verify, so high-tier tools hand off (fail closed). Lower tiers keep working. |
| Number on file was recently SIM-swapped or ported (per the deployer's risk-signal provider) | No code is sent; handoff. The model isn't told why. |
| Contact details changed, then a payment requested on the same call | `require_human` (`contact_change_then_payment`). |
| Caller pressures the agent (urgency + authority + redirect) | Payment tools return `require_human`; the agent offers a follow-up; `policy_handoff` audited. Lower-tier tools keep working. |
| Model calls a tool that doesn't exist or isn't granted | `denied` before anything runs; `policy_denied` audited. |
| Model loops or a caller drip-feeds requests | Velocity denies or hands off; per-call caps hand off at the ceiling. |
| Stripe / calendar / Zendesk / CRM down | Generic "didn't go through", details in the audit log; the attempt doesn't count against the caller. |
| Keypad mode: the call recording can't be paused | `error`; no card capture starts, and the attempt doesn't count against the caller. |
| Keypad mode: the call returns from Twilio `<Pay>` on a different instance | The conversation continues with fresh per-call state; `resume_state_missing` is audited. Velocity per caller still applies. |
| Payment link created but SMS fails | `sms_failed`; the idempotency key is kept so a retry can't create a second link. |
| Same tool call replayed | `duplicate`; nothing runs twice. |
| Amount over the phone limit or a bad timestamp | `rejected` with the reason, so the agent can ask for a correction. |
| Caller goes silent | After `CALL_IDLE_PROMPT_SECONDS` the agent asks if they're still there; after `CALL_IDLE_TIMEOUT_SECONDS` it says goodbye and the call ends (`call_ended_by_limit`, reason `idle`). |
| Call runs past `CALL_MAX_DURATION_SECONDS` | A wrap-up warning a minute before, then a goodbye and the call ends (`reason: max_duration`), including time spent in a keypad payment. |
| Call drops mid-conversation | The post-call summary still runs from whatever was said. |
| DynamoDB unavailable | Idempotency fails **open** and logs an error (keeps the agent usable; flip to fail-closed for payment-only deployments). |
| Several voice instances | With the default in-memory store, velocity limits apply per instance. Set `VELOCITY_BACKEND=redis` to share them; per-call caps are unaffected either way. |
| Redis (shared velocity store) unreachable | Tool calls that have velocity rules are denied (fail closed) with `velocity_denied` / "store unavailable" in the audit log. |
| Speech-to-speech transcript lags the model | A risky sentence in the same turn as a tool call may not be scored yet; caps and velocity still apply (ADR 0001). |
| The policy file is invalid (bad tier, unknown key, regex that doesn't compile, ...) | The voice process refuses to start and names each bad field. |
| Someone calls the tool Lambdas directly | `401`: every tool request must carry an HMAC-SHA256 signature over timestamp, idempotency key and body (`TOOL_API_SECRET`). A captured request can't be altered, replayed under a new idempotency key, or replayed after 5 minutes. Handlers refuse all calls if the secret isn't configured. |

## Call limits

Maximum call duration defaults to 15 minutes, with a wrap-up warning a minute before. The idle timeout asks "are you still there?" after 15 seconds of silence and ends the call at 30 seconds. At a limit the agent says a short goodbye, then the pipeline ends and the call hangs up; `call_ended_by_limit` is audited. Setting a limit to 0 turns it off. The variables are `CALL_MAX_DURATION_SECONDS`, `CALL_DURATION_WARNING_SECONDS`, `CALL_IDLE_PROMPT_SECONDS`, `CALL_IDLE_TIMEOUT_SECONDS` and `CALL_GOODBYE_GRACE_SECONDS` (defaults in `.env.example`). Code: `src/agent/call_limits.py`; evidence: `tests/test_call_limits.py`.

## Observability

Tracing is off by default. Setting `OTEL_EXPORTER_OTLP_ENDPOINT` turns on Pipecat's OpenTelemetry tracing and adds every safeguard decision as a span event (`safeguard.policy`, `safeguard.velocity`, `safeguard.pii_scrubbed`, `safeguard.tool_result`) carrying codes and scores only. The audit log remains the evidence record; traces are the operational view. Pipecat's own spans carry transcript text, so they need a trusted backend or a collector that strips content. Full detail, backend setup and the latency method: [`observability.md`](observability.md).

No latency or cost numbers are published: none have been measured here, and they depend on provider, region and network.

## Deployment options

| Option | Use | Reference |
|---|---|---|
| AWS Lambda (SAM) + Fly.io or ECS | Production: webhook and tool handlers on Lambda with DynamoDB idempotency; the long-lived voice process on Fly.io or ECS | [`deploy.md`](deploy.md), ADR 0003 |
| Local voice process behind a tunnel | Development against a real Twilio number | [`setup.md`](setup.md) |
| Console, demo mode | Static files on GitHub Pages or any static server; the real safeguard code in the browser | [`console.md`](console.md) |
| Console, live mode | `docker compose up`; the real Lambda handler code behind signed requests, with eval fakes for outside services | [`console.md`](console.md) |

Rough running cost and the pre-go-live checks are in [`deploy.md`](deploy.md); the deployment checklist for regulated workloads is in [`compliance.md`](compliance.md#deployment-checklist).
