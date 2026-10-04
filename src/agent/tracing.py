# Corey Mathie, 2026
"""
OpenTelemetry tracing for the voice process.

Off unless OTEL_EXPORTER_OTLP_ENDPOINT (or OTEL_EXPORTER_OTLP_TRACES_ENDPOINT)
is set. When it is, Pipecat's own tracing is configured with an OTLP exporter
(pipecat.utils.tracing.setup.setup_tracing) and bot.py passes enable_tracing=True
to the PipelineWorker. Pipecat then emits a `conversation` span per call, a
`turn` span per exchange (with `turn.user_bot_latency_seconds`), and stt / llm /
tts child spans (with `metrics.ttfb`). Safeguard decisions are added as span
events by src/safeguards/telemetry.py.

Exporter protocol follows OTEL_EXPORTER_OTLP_PROTOCOL: "http/protobuf" (default)
or "grpc" (needs opentelemetry-exporter-otlp-proto-grpc). Endpoint, headers, and
TLS settings are read by the exporter from the standard OTEL_EXPORTER_OTLP_*
variables, so Langfuse, Phoenix, Jaeger, or a collector work without code changes.

Privacy: Pipecat's spans include conversation content (transcripts, LLM output,
the system prompt). Send them only to a backend you'd trust with call
transcripts, or drop those attributes in an OpenTelemetry Collector. See
docs/observability.md.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

_state: dict[str, bool] = {}


def otlp_endpoint() -> str | None:
    return os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")


def _exporter():
    protocol = os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf").strip().lower()
    if protocol == "grpc":
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    else:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    return OTLPSpanExporter()  # endpoint and headers come from OTEL_EXPORTER_OTLP_* env


def configure_tracing(service_name: str = "voice-agent") -> bool:
    """Set up Pipecat's tracer provider with an OTLP exporter. Returns True if tracing is on."""
    if not otlp_endpoint():
        return False
    try:
        from pipecat.utils.tracing.setup import is_tracing_available, setup_tracing

        if not is_tracing_available():
            log.warning("OTEL endpoint set but opentelemetry-sdk is not installed; tracing stays off")
            return False
        exporter = _exporter()
    except ImportError as e:
        log.warning("OTEL endpoint set but an exporter package is missing (%s); tracing stays off", e)
        return False
    ok = setup_tracing(
        service_name=os.environ.get("OTEL_SERVICE_NAME", service_name),
        exporter=exporter,
        console_export=os.environ.get("OTEL_CONSOLE_EXPORT", "").lower() in ("1", "true", "yes"),
    )
    if ok:
        log.info("OpenTelemetry tracing on, exporting to %s", otlp_endpoint())
    return bool(ok)


def tracing_enabled() -> bool:
    """Configure tracing once per process (the tracer provider is global) and report whether it's on."""
    if "enabled" not in _state:
        _state["enabled"] = configure_tracing()
    return _state["enabled"]


def reset_for_tests() -> None:
    _state.clear()
