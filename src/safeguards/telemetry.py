# Corey Mathie, 2026
"""
Safeguard decisions as OpenTelemetry span events.

record_safeguard_event() adds an event to the current span (for example the
Pipecat turn or LLM span the tool call runs under). If there is no recording
span, it emits a short standalone span with the same attributes so the decision
is still visible, correlated by `conversation.id`.

It no-ops when opentelemetry isn't installed or no tracer provider is set up,
and it never raises: telemetry must not break a call.

Only codes, counts, tiers, and scores go into attributes. No transcript text,
arguments, or phone numbers.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

try:  # optional dependency
    from opentelemetry import trace as _trace
except ImportError:  # pragma: no cover - exercised in the browser demo and minimal installs
    _trace = None

TRACER_NAME = "voice_agent.safeguards"


def _clean(attributes: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in attributes.items():
        if value is None:
            continue
        if isinstance(value, str | bool | int | float):
            out[key] = value
        elif isinstance(value, list | tuple) and all(isinstance(v, str) for v in value):
            out[key] = list(value)
        else:
            out[key] = str(value)
    return out


def tracing_available() -> bool:
    return _trace is not None


def record_safeguard_event(name: str, attributes: Mapping[str, Any]) -> bool:
    """Record one safeguard decision. Returns True if it was handed to OpenTelemetry."""
    if _trace is None:
        return False
    try:
        attrs = _clean(attributes)
        span = _trace.get_current_span()
        if span.is_recording():
            span.add_event(name, attrs)
            return True
        with _trace.get_tracer(TRACER_NAME).start_as_current_span(name, attributes=attrs) as standalone:
            return standalone.is_recording()
    except Exception:  # noqa: BLE001 - telemetry must never break a call
        return False
