# ADR 0003: Tools on Lambda, one long-lived worker per call

- Status: accepted
- Date: 2026-10
- Code: `src/handlers/`, `infra/template.yaml`, `src/agent/bot.py`, `Dockerfile`, `fly.toml`

## Context

A phone call is a WebSocket that stays open for minutes and streams 8 kHz audio both ways. Tool calls are short request/response operations against outside services. They have different runtime needs.

## Decision

- **Voice process (Fly.io or ECS):** a long-lived FastAPI + Pipecat process. One `PipelineWorker` per call holds the Twilio media stream, the model session, and the call's in-memory state: the `CallPolicy` (risk score, per-call caps) and the tool outcomes for the summary.
- **Tool handlers (AWS Lambda behind an HTTP API):** `book_meeting`, `take_payment`, `create_ticket`, `log_lead`, `call_summary`, plus the Twilio voice webhook. Each one is short, stateless, and idempotent: it claims the tool-call id in DynamoDB with a conditional write, releases it on failure, and keeps it for 24 hours on success.
- The voice process calls tools over HTTPS with an `Idempotency-Key` header.

## Consequences

- Positive: the webhook and tools scale to zero and are deployed and permissioned separately (each Lambda gets only its secrets).
- Positive: per-call policy state is correct by construction. A call lives on one process, so per-call caps don't need shared storage.
- Negative: velocity state (per caller, across calls) is in process memory. With more than one voice instance, limits apply per instance until the store moves to Redis or DynamoDB. Documented in README "Failure modes".
- Negative, mitigated: the tool routes are public HTTPS endpoints. Since 0.5.0 every request is HMAC-signed by the voice process (`TOOL_API_SECRET`, see `src/handlers/_common.py`) and handlers reject anything unsigned, altered, re-keyed or older than 5 minutes, failing closed when the secret is missing. A shared secret is not workload identity; IAM auth (SigV4) or private networking is the next step for high-value deployments.
- Negative: two deploy targets to operate.
