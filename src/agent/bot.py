# Corey Mathie, 2026
"""
Pipecat pipeline for one phone call over a Twilio Media Stream.

One call = one PipelineWorker. The worker ends when the caller hangs up, and
then the call is summarized for the CRM (see summary.py).
Written against Pipecat 1.12.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)
from pipecat.workers.runner import WorkerRunner

from .provider import get_provider
from .summary import KICKOFF_MESSAGE, finalize_call
from .tools import TOOL_NAMES, build_tools_schema, make_handler

TWILIO_SAMPLE_RATE = 8000  # Twilio Media Streams are 8 kHz mu-law


@dataclass
class CallSession:
    task: PipelineWorker
    transport: FastAPIWebsocketTransport
    context: LLMContext
    outcomes: list[dict] = field(default_factory=list)  # {"tool", "status"} per tool call


def build_pipeline(websocket, stream_sid: str, call_sid: str | None, caller_id: str) -> CallSession:
    """Assemble transport, services, tools, and context for one call."""
    services = get_provider().build_services()

    account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
    serializer = TwilioFrameSerializer(
        stream_sid=stream_sid,
        call_sid=call_sid,
        account_sid=account_sid,
        auth_token=auth_token,
        # Auto hang-up ends the phone call when the pipeline ends; it needs Twilio REST credentials.
        params=TwilioFrameSerializer.InputParams(auto_hang_up=bool(call_sid and account_sid and auth_token)),
    )
    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            serializer=serializer,
        ),
    )

    llm = services["llm"]
    outcomes: list[dict] = []
    # Every tool goes through the safeguard layer, bound to this caller.
    for name in TOOL_NAMES:
        llm.register_function(name, make_handler(name, caller_id, outcomes=outcomes))

    context = LLMContext(
        messages=[{"role": "user", "content": KICKOFF_MESSAGE}],
        tools=build_tools_schema(),
    )
    aggregators = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()),
    )

    stages = [transport.input()]
    if "stt" in services:
        stages.append(services["stt"])
    stages += [aggregators.user(), llm]
    if "tts" in services:
        stages.append(services["tts"])
    stages += [transport.output(), aggregators.assistant()]

    task = PipelineWorker(
        Pipeline(stages),
        params=PipelineParams(
            audio_in_sample_rate=TWILIO_SAMPLE_RATE,
            audio_out_sample_rate=TWILIO_SAMPLE_RATE,
            enable_metrics=True,
        ),
    )

    @transport.event_handler("on_client_connected")
    async def _on_connected(_transport, _client):
        await task.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def _on_disconnected(_transport, _client):
        await task.cancel()

    return CallSession(task=task, transport=transport, context=context, outcomes=outcomes)


async def run_bot(websocket, stream_sid: str, call_sid: str | None, caller_id: str) -> None:
    call = build_pipeline(websocket, stream_sid, call_sid, caller_id)
    runner = WorkerRunner(handle_sigint=False)
    try:
        await runner.add_workers(call.task)
        await runner.run()  # ends when the call's pipeline ends
    finally:
        # Summarize whatever was said and done, even if the call ended abruptly.
        await finalize_call(call.context.get_messages(), call.outcomes, call_sid, caller_id)
