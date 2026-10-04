# Observability

Tracing is **off by default**. Set `OTEL_EXPORTER_OTLP_ENDPOINT` and the voice process turns on Pipecat's OpenTelemetry tracing and adds safeguard decisions as span events. Nothing else changes.

```bash
pip install -r requirements.txt   # includes pipecat-ai[tracing] and the OTLP/HTTP exporter
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318   # any OTLP/HTTP endpoint
export OTEL_SERVICE_NAME=voice-agent                        # optional
python -m src.agent.server
```

How it's wired:

- `src/agent/tracing.py` builds an `OTLPSpanExporter` (HTTP/protobuf by default, gRPC with `OTEL_EXPORTER_OTLP_PROTOCOL=grpc` and the gRPC exporter installed) and passes it to Pipecat's `setup_tracing()`. Endpoint, headers, and TLS come from the standard `OTEL_EXPORTER_OTLP_*` variables. It runs once per process.
- `src/agent/bot.py` creates each call's `PipelineWorker` with `enable_tracing=True` and `conversation_id=<Twilio CallSid>`, so a trace lines up with the audit log and Twilio's call logs.
- `src/safeguards/telemetry.py` records each safeguard decision. If the tool call runs under a recording span, the decision is added to it as an event. If not, a short standalone `safeguard.*` span is emitted carrying `conversation.id`, so it can still be joined to the call. It does nothing when OpenTelemetry isn't installed or no provider is configured, and never raises.

Tests: `tests/test_observability.py`, `tests/test_bot_wiring.py::test_tracing_flag_reaches_the_pipeline_worker`.

## What you get

**Pipecat spans** (from Pipecat 1.12's tracing; names and attributes are Pipecat's):

| Span | Key attributes |
|---|---|
| `conversation` (one per call) | `conversation.id` (the CallSid), `conversation.type`, `voice_agent.provider` (added here) |
| `turn` (one per exchange) | `turn.number`, `turn.duration_seconds`, `turn.was_interrupted`, `turn.user_bot_latency_seconds` |
| `stt`, `llm`, `tts`, realtime-service spans | `metrics.ttfb`, `gen_ai.provider.name`, `gen_ai.request.model`, token usage, and content attributes (see privacy below) |

**Safeguard events** (added by this repo):

| Event | Attributes |
|---|---|
| `safeguard.policy` | `tool.name`, `safeguard.decision` (allow / deny / handoff), `safeguard.code` (e.g. `social_engineering_risk`, `usd_cap`, `unknown_tool`), `policy.tier`, `policy.risk_score`, `policy.risk_signals`, `conversation.id` |
| `safeguard.velocity` | `tool.name`, `safeguard.decision` |
| `safeguard.pii_scrubbed` | `tool.name`, `pii.removed` (count), `pii.rules` (e.g. `pan`, `ssn`) |
| `safeguard.tool_result` | `tool.name`, `tool.status` |

Safeguard events carry codes, counts, tiers, and scores only: no transcript text, arguments, or phone numbers. A tool name the model invented is reported as `<unlisted>`.

## Privacy: Pipecat spans contain conversation content

Pipecat's service spans include attributes such as `transcript`, `output`, `text`, and `gen_ai.system_instructions`. With tracing on, **caller speech goes to your tracing backend**. Either send traces only to a backend you'd trust with call transcripts (self-hosted, access-controlled, with a retention policy), or drop those attributes in an OpenTelemetry Collector before export:

```yaml
processors:
  attributes/strip-content:
    actions:
      - { key: transcript, action: delete }
      - { key: output, action: delete }
      - { key: text, action: delete }
      - { key: gen_ai.system_instructions, action: delete }
```

## Viewing traces

The endpoints below are each backend's documented OTLP ingest at the time of writing; check the backend's current docs.

**Jaeger (local).**

```bash
docker run --rm -p 16686:16686 -p 4318:4318 jaegertracing/all-in-one:latest
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
```

Open http://localhost:16686, pick service `voice-agent`, and open a trace: `conversation` → `turn` → service spans. Safeguard decisions appear as span logs/events or as `safeguard.*` spans.

**Arize Phoenix (local).**

```bash
docker run --rm -p 6006:6006 arizephoenix/phoenix:latest
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:6006
```

**Langfuse (cloud or self-hosted).** Langfuse accepts OTLP/HTTP at `/api/public/otel` with Basic auth from your project keys:

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=https://cloud.langfuse.com/api/public/otel
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Basic%20$(printf '%s:%s' "$LANGFUSE_PUBLIC_KEY" "$LANGFUSE_SECRET_KEY" | base64 -w0)"
```

Useful queries once data is flowing: filter spans by `safeguard.decision = handoff` to review every escalation, group by `safeguard.code` to see which control fires most, and compare `policy.risk_score` on handed-off calls with what the caller actually wanted (from the audit log and CRM note) to tune `POLICY_RISK_THRESHOLD`.

## Time-to-first-audio: p50 / p95

**Definition used here:** `turn.user_bot_latency_seconds`, which Pipecat's `UserBotLatencyObserver` measures inside the voice process from the moment the caller stopped speaking (the VAD's stop time, adjusted back by the VAD `stop_secs` window) to the bot's first audio frame (`BotStartedSpeakingFrame`). It includes endpointing wait, STT, model, and TTS time. It **excludes** the Twilio media-stream hop and the phone network in both directions, so the caller's perceived delay is higher.

`evals/latency.py` computes percentiles from data you export:

```bash
# OpenTelemetry Collector `file` exporter (OTLP/JSON lines), or a single OTLP/JSON export
python -m evals.latency traces.jsonl

# Jaeger: download trace JSON from the UI, or use the query API
curl -s "http://localhost:16686/api/traces?service=voice-agent&limit=500" > jaeger.json
python -m evals.latency jaeger.json

# Pipecat debug logs with tracing on ("Turn N user-bot latency: 0.812s")
python -m evals.latency --format log voice.log
```

Output is a table of n, p50, p95, p99, and max for `time_to_first_audio_s` plus each service's `<span>.ttfb_s`. Percentiles use linear interpolation between ranks (numpy's default). With fewer than 20 samples the script warns that p95/p99 aren't meaningful.

**No latency figures are published in this repo.** None have been measured here: that needs live calls with real provider keys. Results depend on provider and model, region, network path, VAD settings, and prompt length, so report them with those conditions attached.

## Audit log vs traces

| | Audit log (`AUDIT_LOG_PATH`) | Traces |
|---|---|---|
| Purpose | Evidence: what the agent did and why | Operations: latency, errors, flow |
| Content | Redacted; hash-chained; every decision | Pipecat spans include content; safeguard events don't |
| Always on | Yes | Only with `OTEL_EXPORTER_OTLP_ENDPOINT` |
| Join key | `caller_ref`, `idempotency_key`, `call_sid` on summaries | `conversation.id` = CallSid |
