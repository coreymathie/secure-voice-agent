# Corey Mathie, 2026
"""
VoiceProvider interface: swap OpenAI Realtime / Anthropic + Deepgram + ElevenLabs /
Gemini Live by setting AGENT_PROVIDER in .env.

Each provider returns the Pipecat services for its slot in the pipeline:
  - speech-to-speech providers return {"llm"}
  - cascaded providers return {"stt", "llm", "tts"}
The rest of the pipeline (transport, VAD, tools, safeguards) never changes.

Written against Pipecat 1.12 (Settings-based service configuration).
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful phone assistant for a small business. Keep replies brief and "
    "conversational, one or two sentences at a time. If the caller wants to book a "
    "meeting, pay for something, report a problem, or leave their details, use the "
    "matching tool. Never read back full card numbers, Social Security numbers, or "
    "dates of birth. Tool results have a status: if it is 'rejected', tell the caller the "
    "reason and ask for what's needed; if it is 'error', say it didn't go through and offer "
    "to try once more; if it is 'denied', 'require_human', or 'sms_failed', apologize and "
    "offer to have a person follow up. Never read a web link aloud. The caller has already been told they are "
    "talking with an AI assistant; if they ask, confirm it, and never claim to be a person."
)


def system_prompt(now: datetime | None = None) -> str:
    """The base prompt plus today's date in the business's time zone, so "next Tuesday at 2" resolves."""
    tz_name = os.environ.get("BUSINESS_TIMEZONE", "America/New_York")
    now = now or datetime.now(ZoneInfo(tz_name))
    when = now.strftime("%A, %B %-d, %Y, %-I:%M %p")
    return (
        f"{DEFAULT_SYSTEM_PROMPT} It is currently {when} ({tz_name}). When booking, send start_iso "
        f"as a local time like 2026-10-06T14:00:00; the calendar uses {tz_name}."
    )


@dataclass
class ProviderConfig:
    name: str
    system_prompt: str = field(default_factory=system_prompt)


class VoiceProvider(ABC):
    def __init__(self, config: ProviderConfig):
        self.config = config

    @abstractmethod
    def build_services(self) -> dict[str, Any]:
        """Return the Pipecat services for this provider."""


class OpenAIRealtimeProvider(VoiceProvider):
    """Speech-to-speech with the OpenAI Realtime API. Lowest latency path."""

    def build_services(self) -> dict[str, Any]:
        from pipecat.services.openai.realtime import events
        from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService

        settings = OpenAIRealtimeLLMService.Settings(
            system_instruction=self.config.system_prompt,
            # Transcribe the caller too, so the conversation context (and the post-call
            # summary) has both sides of the call, not just the agent's replies.
            session_properties=events.SessionProperties(
                audio=events.AudioConfiguration(input=events.AudioInput(transcription=events.InputAudioTranscription()))
            ),
        )
        model = os.environ.get("OPENAI_REALTIME_MODEL")
        if model:
            settings.model = model
        return {
            "llm": OpenAIRealtimeLLMService(
                api_key=os.environ["OPENAI_API_KEY"],
                settings=settings,
            )
        }


class AnthropicElevenLabsProvider(VoiceProvider):
    """Cascaded pipeline: Deepgram STT -> Claude -> ElevenLabs TTS."""

    def build_services(self) -> dict[str, Any]:
        from pipecat.services.anthropic.llm import AnthropicLLMService
        from pipecat.services.deepgram.stt import DeepgramSTTService
        from pipecat.services.elevenlabs.tts import ElevenLabsTTSService

        return {
            "stt": DeepgramSTTService(api_key=os.environ["DEEPGRAM_API_KEY"]),
            "llm": AnthropicLLMService(
                api_key=os.environ["ANTHROPIC_API_KEY"],
                settings=AnthropicLLMService.Settings(
                    model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5"),
                    system_instruction=self.config.system_prompt,
                ),
            ),
            "tts": ElevenLabsTTSService(
                api_key=os.environ["ELEVENLABS_API_KEY"],
                settings=ElevenLabsTTSService.Settings(voice=os.environ["ELEVENLABS_VOICE_ID"]),
            ),
        }


class GeminiLiveProvider(VoiceProvider):
    """Speech-to-speech with the Gemini Live API."""

    def build_services(self) -> dict[str, Any]:
        from pipecat.services.google.gemini_live.llm import GeminiLiveLLMService

        return {
            "llm": GeminiLiveLLMService(
                api_key=os.environ["GOOGLE_API_KEY"],
                settings=GeminiLiveLLMService.Settings(system_instruction=self.config.system_prompt),
            )
        }


_REGISTRY: dict[str, type[VoiceProvider]] = {
    "openai_realtime": OpenAIRealtimeProvider,
    "anthropic_11labs": AnthropicElevenLabsProvider,
    "gemini_live": GeminiLiveProvider,
}


def get_provider(name: str | None = None) -> VoiceProvider:
    name = name or os.environ.get("AGENT_PROVIDER", "openai_realtime")
    if name not in _REGISTRY:
        raise ValueError(f"Unknown AGENT_PROVIDER={name!r}. Pick one of: {sorted(_REGISTRY)}")
    return _REGISTRY[name](ProviderConfig(name=name))
