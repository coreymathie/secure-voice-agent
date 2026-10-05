# ADR 0001: Speech-to-speech by default, cascaded as a swappable option

- Status: accepted
- Date: 2026-10
- Code: `src/agent/provider.py`, `src/agent/bot.py`

## Context

A phone agent can be built two ways:

- **Speech-to-speech (S2S):** one realtime model takes caller audio and returns agent audio (OpenAI Realtime, Gemini Live). Fewer hops, and the model hears tone and interruptions directly.
- **Cascaded:** speech-to-text, a text LLM, then text-to-speech (here: Deepgram, Claude, ElevenLabs). More hops, but each stage is a separate, replaceable component, and the text in the middle is easy to inspect.

The rest of the system (Twilio transport, VAD, tool registration, the safeguard layer, post-call summaries) should not care which one is used.

## Decision

- Default to S2S (`AGENT_PROVIDER=openai_realtime`), with cascaded (`anthropic_11labs`) and a second S2S option (`gemini_live`) behind the same `VoiceProvider` interface.
- A provider only returns Pipecat services: `{"llm"}` for S2S or `{"stt", "llm", "tts"}` for cascaded. `bot.build_pipeline` inserts STT and TTS only when present. Nothing else changes between providers.
- Safeguards never depend on the model. All tool calls, from any provider, go through `make_handler()` (see ADR 0002).
- OpenAI Realtime is configured to transcribe the caller too, so the LLM context holds both sides of the call. The post-call summary and the policy gate's risk scoring read the caller's words from that context.

## Consequences

- Positive: switching providers is one environment variable; `tests/test_bot_wiring.py::test_each_provider_builds_services` builds all three.
- Positive: the choice can be made per deployment on measured latency, cost, and quality. This repo publishes **no latency numbers**; `docs/observability.md` shows how to measure time-to-first-audio for each provider on your own calls.
- Negative: in S2S mode the caller's transcript is a side output of the realtime model and can arrive after the model has already decided to call a tool. The policy gate scores whatever caller turns are in the context at decision time, so a risky sentence in the same turn as a tool call may not be scored yet. The per-call caps and velocity limits don't depend on the transcript and still apply. In cascaded mode the transcript is finalized before the LLM runs.
- Negative: the cascaded path has three vendors to secure and contract with instead of one.

## Not decided here

Which provider is "fastest" or "cheapest". That depends on region, network, model version, and VAD settings, and should be measured, not assumed.
