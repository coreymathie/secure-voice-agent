# Corey Mathie, 2026
"""
Latency percentiles from exported traces or logs.

    python -m evals.latency traces.json [more files...]
    python -m evals.latency --format log pipecat.log

Reads what Pipecat's OpenTelemetry tracing produces (see docs/observability.md)
and prints p50 / p95 / p99 per metric:

  time_to_first_audio_s   `turn.user_bot_latency_seconds` on each `turn` span:
                          from the end of the caller's speech (VAD stop time
                          minus the VAD stop_secs window) to the bot's first audio
                          frame, measured inside the voice process. It excludes
                          the Twilio and phone-network legs.
  <span>.ttfb_s           `metrics.ttfb` on stt / llm / tts spans: time to the
                          service's first byte.

Accepted inputs (detected per file with --format auto):
  otlp    OTLP/JSON: an ExportTraceServiceRequest object, or one per line
          (OpenTelemetry Collector `file` exporter)
  jaeger  Jaeger UI "Download JSON" / Jaeger query API ({"data": [{"spans": ...}]})
  log     Pipecat debug logs with tracing on ("Turn N user-bot latency: 0.812s")

This script only summarizes data you collected. The repo publishes no latency
numbers: they depend on provider, region, network, and VAD settings.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import defaultdict
from collections.abc import Iterable, Iterator
from pathlib import Path

TTFA = "time_to_first_audio_s"
_LOG_LINE = re.compile(r"user-bot latency: ([0-9]+(?:\.[0-9]+)?)s")


def percentile(values: list[float], q: float) -> float:
    """Linear interpolation between closest ranks (same as numpy's default)."""
    if not values:
        raise ValueError("no values")
    xs = sorted(values)
    pos = (len(xs) - 1) * q / 100.0
    lo, hi = math.floor(pos), math.ceil(pos)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def _otlp_value(v: dict):
    for key in ("doubleValue", "intValue", "stringValue", "boolValue"):
        if key in v:
            return v[key]
    return None


def _otlp_spans(doc: dict) -> Iterator[tuple[str, dict]]:
    for rs in doc.get("resourceSpans", []):
        for ss in rs.get("scopeSpans", rs.get("instrumentationLibrarySpans", [])):
            for span in ss.get("spans", []):
                attrs = {a["key"]: _otlp_value(a.get("value", {})) for a in span.get("attributes", [])}
                yield span.get("name", ""), attrs


def _jaeger_spans(doc: dict) -> Iterator[tuple[str, dict]]:
    for trace in doc.get("data", []):
        for span in trace.get("spans", []):
            attrs = {t["key"]: t.get("value") for t in span.get("tags", [])}
            yield span.get("operationName", ""), attrs


def _number(v) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) and x >= 0 else None


def samples_from_spans(spans: Iterable[tuple[str, dict]]) -> dict[str, list[float]]:
    out: dict[str, list[float]] = defaultdict(list)
    for name, attrs in spans:
        if name == "turn":
            x = _number(attrs.get("turn.user_bot_latency_seconds"))
            if x is not None:
                out[TTFA].append(x)
        x = _number(attrs.get("metrics.ttfb"))
        if x is not None and name:
            out[f"{name}.ttfb_s"].append(x)
    return out


def _detect(text: str) -> str:
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        return "otlp" if text.lstrip().startswith("{") else "log"  # JSON lines, or plain logs
    return "jaeger" if isinstance(doc, dict) and "data" in doc else "otlp"


def load_samples(path: Path, fmt: str = "auto") -> dict[str, list[float]]:
    text = path.read_text()
    fmt = _detect(text) if fmt == "auto" else fmt
    if fmt == "log":
        return {TTFA: [float(m.group(1)) for m in _LOG_LINE.finditer(text)]}
    if fmt == "jaeger":
        return samples_from_spans(_jaeger_spans(json.loads(text)))
    try:
        docs = [json.loads(text)]
    except json.JSONDecodeError:
        docs = [json.loads(line) for line in text.splitlines() if line.strip()]
    spans: list[tuple[str, dict]] = []
    for doc in docs:
        spans += list(_otlp_spans(doc))
    return samples_from_spans(spans)


def summarize(samples: dict[str, list[float]]) -> list[dict]:
    rows = []
    for metric in sorted(samples, key=lambda m: (m != TTFA, m)):
        xs = samples[metric]
        if not xs:
            continue
        rows.append(
            {
                "metric": metric,
                "n": len(xs),
                "p50": percentile(xs, 50),
                "p95": percentile(xs, 95),
                "p99": percentile(xs, 99),
                "max": max(xs),
            }
        )
    return rows


def table(rows: list[dict]) -> str:
    lines = ["| metric | n | p50 (s) | p95 (s) | p99 (s) | max (s) |", "|---|---:|---:|---:|---:|---:|"]
    for r in rows:
        lines.append(
            f"| {r['metric']} | {r['n']} | {r['p50']:.3f} | {r['p95']:.3f} | {r['p99']:.3f} | {r['max']:.3f} |"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Latency percentiles from Pipecat traces or logs")
    ap.add_argument("files", nargs="+", type=Path)
    ap.add_argument("--format", choices=["auto", "otlp", "jaeger", "log"], default="auto")
    ap.add_argument("--json", action="store_true", help="print JSON instead of a table")
    args = ap.parse_args(argv)

    merged: dict[str, list[float]] = defaultdict(list)
    for f in args.files:
        for metric, xs in load_samples(f, args.format).items():
            merged[metric] += xs
    rows = summarize(merged)
    if not rows:
        print("no latency samples found (is tracing on? see docs/observability.md)", file=sys.stderr)
        return 2
    print(json.dumps(rows, indent=2) if args.json else table(rows))
    small = [r["metric"] for r in rows if r["n"] < 20]
    if small and not args.json:
        print(f"\nnote: fewer than 20 samples for {', '.join(small)}; p95/p99 are not meaningful yet", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
