# ADR 0003: Tools on Lambda, one long-lived worker per call

- **Status:** accepted
- **Date:** 2026-10
- **Code:** `src/handlers/`, `infra/template.yaml`, `src/agent/bot.py`, `Dockerfile`, `fly.toml`

## Context

A phone call is a WebSocket that stays open for minutes and streams 8 kHz audio both ways. Tool calls are short request/response operations against outside services. The two workloads have different runtime, scaling and permission needs, and the tool side is where money moves, so it needs the tightest permission boundary.

## Decision

- **Voice process (Fly.io or ECS):** a long-lived FastAPI + Pipecat process. One `PipelineWorker` per call holds the Twilio media stream, the model session, and the call's in-memory state: the `CallPolicy` (risk score, per-call caps) and the tool outcomes for the summary.
- **Tool handlers (AWS Lambda behind an HTTP API):** `book_meeting`, `take_payment`, `create_ticket`, `log_lead`, `call_summary`, plus the Twilio voice webhook. Each one is short, stateless, and idempotent: it claims the tool-call id in DynamoDB with a conditional write, releases it on failure, and keeps it for 24 hours on success. Later releases added `update_contact` and the keypad-payment handlers on the same pattern (see [`docs/architecture.md`](../architecture.md)).
- The voice process calls tools over HTTPS with an `Idempotency-Key` header, and since 0.5.0 with an HMAC signature.

## Consequences

**Positive**

- The webhook and tools scale to zero and are deployed and permissioned separately (each Lambda gets only its secrets), which limits the blast radius of any one credential.
- Per-call policy state is correct by construction. A call lives on one process, so per-call caps don't need shared storage.

**Negative**

- Velocity state (per caller, across calls) is in process memory by default. With more than one voice instance, limits apply per instance unless a shared store is configured; since 0.6.0, `VELOCITY_BACKEND=redis` provides one (fails closed if Redis is unreachable). Documented in [`docs/operations.md`](../operations.md#failure-modes).
- Mitigated: the tool routes are public HTTPS endpoints. Since 0.5.0 every request is HMAC-signed by the voice process (`TOOL_API_SECRET`, see `src/handlers/_common.py`) and handlers reject anything unsigned, altered, re-keyed or older than 5 minutes, failing closed when the secret is missing. A shared secret is not workload identity; IAM auth (SigV4) or private networking is the next step for high-value deployments.
- Two deploy targets to operate.

## Alternatives considered

- **Everything in the long-lived voice process.** One deploy target, but backend credentials would sit in the process that handles untrusted audio and model output, and tools could not be permissioned or scaled separately.
- **Everything on Lambda.** Not viable for the call itself: a phone call is a long-lived WebSocket that needs a persistent process.
