# Corey Mathie, 2026
"""Tracing setup, safeguard span events, and the latency percentile script."""

import json
from types import SimpleNamespace

import pytest

from evals import latency
from src.agent import tracing
from src.safeguards import telemetry

# ---------- Tracing setup ----------


@pytest.fixture(autouse=True)
def clean_tracing_state(monkeypatch):
    for k in ("OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "OTEL_EXPORTER_OTLP_PROTOCOL"):
        monkeypatch.delenv(k, raising=False)
    tracing.reset_for_tests()
    yield
    tracing.reset_for_tests()


def test_tracing_is_off_without_an_endpoint():
    assert tracing.configure_tracing() is False
    assert tracing.tracing_enabled() is False


def test_tracing_uses_pipecat_setup_with_an_otlp_exporter(monkeypatch):
    pytest.importorskip("pipecat")
    pytest.importorskip("opentelemetry.sdk")
    pytest.importorskip("opentelemetry.exporter.otlp.proto.http")
    import pipecat.utils.tracing.setup as setup

    seen = {}

    def fake_setup(service_name, exporter=None, console_export=False):
        seen.update(service_name=service_name, exporter=exporter)
        return True

    monkeypatch.setattr(setup, "setup_tracing", fake_setup)
    monkeypatch.setattr(setup, "is_tracing_available", lambda: True)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "voice-agent-test")
    assert tracing.tracing_enabled() is True
    assert seen["service_name"] == "voice-agent-test"
    assert type(seen["exporter"]).__name__ == "OTLPSpanExporter"
    # Configured once per process: the tracer provider is global.
    seen.clear()
    assert tracing.tracing_enabled() is True and not seen


def test_tracing_stays_off_if_exporter_is_missing(monkeypatch):
    pytest.importorskip("pipecat")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc")

    def missing():
        raise ImportError("opentelemetry-exporter-otlp-proto-grpc")

    monkeypatch.setattr(tracing, "_exporter", missing)
    assert tracing.configure_tracing() is False


# ---------- Safeguard span events ----------


def test_safeguard_event_lands_on_the_current_span():
    sdk = pytest.importorskip("opentelemetry.sdk.trace")
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = sdk.TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    with provider.get_tracer("test").start_as_current_span("turn"):
        assert telemetry.record_safeguard_event(
            "safeguard.policy",
            {"safeguard.decision": "handoff", "policy.risk_score": 6, "policy.risk_signals": ["urgency"], "x": None},
        )
    (span,) = exporter.get_finished_spans()
    (event,) = span.events
    assert event.name == "safeguard.policy"
    assert dict(event.attributes) == {
        "safeguard.decision": "handoff",
        "policy.risk_score": 6,
        "policy.risk_signals": ("urgency",),
    }


def test_safeguard_event_is_a_noop_without_a_provider():
    # No SDK provider is installed globally in tests, so there's no recording span.
    assert telemetry.record_safeguard_event("safeguard.policy", {"a": 1}) is False


def test_safeguard_event_without_opentelemetry(monkeypatch):
    monkeypatch.setattr(telemetry, "_trace", None)
    assert telemetry.tracing_available() is False
    assert telemetry.record_safeguard_event("safeguard.policy", {"a": 1}) is False


def test_safeguard_event_never_raises(monkeypatch):
    def boom():
        raise RuntimeError("exporter exploded")

    monkeypatch.setattr(telemetry, "_trace", SimpleNamespace(get_current_span=boom))
    assert telemetry.record_safeguard_event("safeguard.policy", {"a": object()}) is False


# ---------- Latency percentiles ----------
# The values below are synthetic test fixtures, not measurements.


def test_percentile_matches_linear_interpolation():
    xs = [1.0, 2.0, 3.0, 4.0]
    assert latency.percentile(xs, 50) == 2.5
    assert latency.percentile(xs, 100) == 4.0
    assert latency.percentile([7.0], 95) == 7.0
    with pytest.raises(ValueError):
        latency.percentile([], 50)


def _otlp(spans):
    def attr(k, v):
        key = "doubleValue" if isinstance(v, float) else "stringValue"
        return {"key": k, "value": {key: v}}

    return {
        "resourceSpans": [
            {
                "scopeSpans": [
                    {"spans": [{"name": n, "attributes": [attr(k, v) for k, v in a.items()]} for n, a in spans]}
                ]
            }
        ]
    }


def test_otlp_json_and_json_lines(tmp_path):
    doc = _otlp(
        [
            ("turn", {"turn.user_bot_latency_seconds": 1.0}),
            ("turn", {"turn.user_bot_latency_seconds": 3.0}),
            ("llm", {"metrics.ttfb": 0.5}),
            ("conversation", {"conversation.id": "CA1"}),
        ]
    )
    single = tmp_path / "t.json"
    single.write_text(json.dumps(doc))
    lines = tmp_path / "t.jsonl"
    lines.write_text(json.dumps(doc) + "\n" + json.dumps(doc) + "\n")
    s = latency.load_samples(single)
    assert s == {latency.TTFA: [1.0, 3.0], "llm.ttfb_s": [0.5]}
    assert latency.load_samples(lines)[latency.TTFA] == [1.0, 3.0, 1.0, 3.0]


def test_jaeger_json(tmp_path):
    f = tmp_path / "jaeger.json"
    f.write_text(
        json.dumps(
            {
                "data": [
                    {
                        "spans": [
                            {"operationName": "turn", "tags": [{"key": "turn.user_bot_latency_seconds", "value": 2.0}]},
                            {"operationName": "tts", "tags": [{"key": "metrics.ttfb", "value": 0.25}]},
                        ]
                    }
                ]
            }
        )
    )
    assert latency.load_samples(f) == {latency.TTFA: [2.0], "tts.ttfb_s": [0.25]}


def test_log_lines(tmp_path):
    f = tmp_path / "bot.log"
    f.write_text("DEBUG Turn 1 user-bot latency: 0.900s\nINFO other\nDEBUG Turn 2 user-bot latency: 1.100s\n")
    assert latency.load_samples(f) == {latency.TTFA: [0.9, 1.1]}


def test_cli_table_and_empty_input(tmp_path, capsys):
    f = tmp_path / "bot.log"
    f.write_text("Turn 1 user-bot latency: 1.000s\nTurn 2 user-bot latency: 2.000s\n")
    assert latency.main([str(f)]) == 0
    out = capsys.readouterr()
    assert "| time_to_first_audio_s | 2 | 1.500 |" in out.out and "fewer than 20" in out.err
    assert latency.main([str(f), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["n"] == 2
    empty = tmp_path / "empty.log"
    empty.write_text("nothing here\n")
    assert latency.main([str(empty)]) == 2
