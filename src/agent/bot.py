# Corey Mathie, 2026
"""
Pipecat pipeline for one phone call over a Twilio Media Stream.

One call = one PipelineWorker. The worker ends when the caller hangs up, and
then the call is summarized for the CRM (see summary.py).

Each call gets one CallPolicy (src/safeguards/policy_gate.py), shared by every
tool handler. Its transcript source reads the caller's finalized turns from the
LLM context at decision time, so the social-engineering score covers everything
the caller has said up to the tool call. A catch-all handler routes any tool name
the model invents to the same gate, which denies it.

Each call also gets one StepUpSession (src/safeguards/step_up.py): the caller's
number is used only to look up the customer record, and a one-time code goes to
the phone on file. Passing it marks the call's CallPolicy verified, which
high-tier tools require.

Keypad payments (PAYMENT_MODE=keypad, docs/pci.md): take_payment redirects the
call to Twilio <Pay>, which ends this media stream. A CaptureGuard right after
the transport input drops keypad tones always, and caller audio/transcripts
while a capture is active. If the stream ends mid-capture, the call's context,
outcomes, policy, and step-up state are parked in resume.SUSPENDED and picked
up by the new stream Twilio opens when <Pay> finishes; the summary runs once,
at the real end of the call.

Tracing: with OTEL_EXPORTER_OTLP_ENDPOINT set, Pipecat's OpenTelemetry tracing is
turned on (conversation -> turn -> stt/llm/tts spans) and safeguard decisions are
added as span events. See docs/observability.md.
Written against Pipecat 1.12.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from dataclasses import dataclass, field

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import EndFrame, LLMMessagesAppendFrame, LLMRunFrame
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

from src.handlers.pay_twiml import RESULT_MESSAGES, payment_mode
from src.safeguards.policy_config import active_policy
from src.safeguards.policy_gate import CallPolicy, PolicyConfig
from src.safeguards.step_up import StepUpConfig, StepUpSession, crm_from_env, verifier_from_env
from src.safeguards.velocity import VelocityStore, velocity_store_from_env

from .call_limits import CallLimits, CallTimer, make_activity_observer, watch_call
from .capture import CaptureState, make_capture_guard
from .disclosure import on_call_start
from .provider import get_provider
from .resume import SUSPENDED
from .summary import KICKOFF_MESSAGE, RESUME_KICKOFF, finalize_call, transcript_from_messages
from .tools import (
    TOOL_NAMES,
    build_tools_schema,
    caller_ref,
    get_audit,
    lambda_executors,
    make_fallback_handler,
    make_handler,
    step_up_executors,
)
from .tracing import tracing_enabled

TWILIO_SAMPLE_RATE = 8000  # Twilio Media Streams are 8 kHz mu-law

_velocity: VelocityStore | None = None


def velocity_store() -> VelocityStore:
    """
    One velocity store per process, with the rules from the active policy file: in memory by
    default, or shared through Redis with VELOCITY_BACKEND=redis (src/safeguards/velocity_redis.py).
    """
    global _velocity
    if _velocity is None:
        _velocity = velocity_store_from_env(active_policy().velocity_rules)
    return _velocity


@dataclass
class CallSession:
    task: PipelineWorker
    transport: FastAPIWebsocketTransport
    context: LLMContext
    outcomes: list[dict] = field(default_factory=list)  # {"tool", "status"} per tool call
    policy: CallPolicy | None = None  # the call's policy-gate state (risk score, per-call caps)
    step_up: StepUpSession | None = None  # the call's step-up verification state
    capture: CaptureState = field(default_factory=CaptureState)  # keypad-payment capture flags
    resumed: bool = False  # this stream picked a call back up after Twilio <Pay>
    timer: CallTimer = field(default_factory=CallTimer)  # max duration and idle timeout


def caller_turns_from(context: LLMContext):
    """Transcript source for the policy gate: the caller's finalized turns, read when a tool is called."""

    def source() -> list[str]:
        return [text for role, text in transcript_from_messages(context.get_messages()) if role == "user"]

    return source


def build_pipeline(
    websocket,
    stream_sid: str,
    call_sid: str | None,
    caller_id: str,
    *,
    resume: str | None = None,
    pay_result: str | None = None,
) -> CallSession:
    """Assemble transport, services, tools, and context for one call (or its continuation after <Pay>)."""
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
    parked = SUSPENDED.resume(call_sid) if resume == "keypad_payment" else None
    if resume == "keypad_payment" and parked is None:
        # Different instance or a restart: carry on, but say so in the audit log.
        get_audit().append("resume_state_missing", {"caller_ref": caller_ref(caller_id), "call_sid": call_sid})
    if parked is not None:
        result = pay_result if pay_result in RESULT_MESSAGES else "unknown"
        status = "keypad_paid" if result == "success" else "keypad_failed"
        outcomes: list[dict] = [*parked.outcomes, {"tool": "take_payment", "status": status}]
        messages = [*parked.messages, {"role": "user", "content": RESUME_KICKOFF.format(result=result)}]
        get_audit().append(
            "keypad_payment_result", {"caller_ref": caller_ref(caller_id), "call_sid": call_sid, "result": result}
        )
    else:
        outcomes = []
        messages = [{"role": "user", "content": KICKOFF_MESSAGE}]
    context = LLMContext(messages=messages, tools=build_tools_schema())
    if parked is not None:
        policy, step_up = parked.policy, parked.step_up
        policy.transcript_source = caller_turns_from(context)
    else:
        loaded = active_policy()  # config/policy.yaml, validated at startup; POLICY_* / STEP_UP_* apply on top
        policy = CallPolicy(
            config=PolicyConfig.from_env(base=loaded.policy),
            transcript_source=caller_turns_from(context),
            call_id=call_sid,
        )
        step_up = StepUpSession(
            caller_id,
            crm_from_env(),
            verifier_from_env(),
            policy=policy,
            config=StepUpConfig.from_env(base=loaded.step_up),
        )
    capture = CaptureState()
    timer = CallTimer(CallLimits.from_env(), started_at=parked.started_at if parked is not None else None)

    def watch(tool: str, result: dict) -> None:
        if tool == "take_payment" and result.get("status") == "keypad_started":
            capture.begin(result.get("recording"))

    executors = {**lambda_executors(payment_mode()), **step_up_executors(step_up)}
    # Every tool goes through the safeguard layer, bound to this caller and this call's policy.
    for name in TOOL_NAMES:
        llm.register_function(
            name,
            make_handler(
                name,
                caller_id,
                executors,
                velocity=velocity_store(),
                outcomes=outcomes,
                policy=policy,
                on_result=watch,
            ),
        )
    # Anything else the model calls lands here and is denied by the gate (default deny).
    llm.register_function(
        None,
        make_fallback_handler(
            caller_id, executors=executors, velocity=velocity_store(), outcomes=outcomes, policy=policy
        ),
    )
    aggregators = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()),
    )

    stages = [transport.input(), make_capture_guard(capture)]
    if "stt" in services:
        stages.append(services["stt"])
    stages += [aggregators.user(), llm]
    if "tts" in services:
        stages.append(services["tts"])
    stages += [transport.output(), aggregators.assistant()]

    tracing = tracing_enabled()
    task = PipelineWorker(
        Pipeline(stages),
        params=PipelineParams(
            audio_in_sample_rate=TWILIO_SAMPLE_RATE,
            audio_out_sample_rate=TWILIO_SAMPLE_RATE,
            enable_metrics=True,
        ),
        enable_tracing=tracing,
        # Feeds caller speech and the agent's speaking state into the call's max-duration / idle timer.
        observers=[make_activity_observer(timer)],
        # The Twilio call SID ties Pipecat's conversation span to the audit log and safeguard events.
        conversation_id=call_sid if tracing else None,
        additional_span_attributes={"voice_agent.provider": os.environ.get("AGENT_PROVIDER", "openai_realtime")}
        if tracing
        else None,
    )

    @transport.event_handler("on_client_connected")
    async def _on_connected(_transport, _client):
        await task.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def _on_disconnected(_transport, _client):
        await task.cancel()

    return CallSession(
        task=task,
        transport=transport,
        context=context,
        outcomes=outcomes,
        policy=policy,
        step_up=step_up,
        capture=capture,
        resumed=parked is not None,
        timer=timer,
    )


async def run_bot(
    websocket,
    stream_sid: str,
    call_sid: str | None,
    caller_id: str,
    *,
    resume: str | None = None,
    pay_result: str | None = None,
    stream_params: dict | None = None,
) -> None:
    call = build_pipeline(websocket, stream_sid, call_sid, caller_id, resume=resume, pay_result=pay_result)
    if stream_params is not None and resume is None:
        get_audit().append(
            "call_started",
            {"caller_ref": caller_ref(caller_id), "call_sid": call_sid, "policy": active_policy().version_ref},
        )
        # Audit the disclosure/consent the TwiML played; start a recording only if consent allows it.
        await asyncio.to_thread(on_call_start, stream_params, call_sid, caller_ref(caller_id), get_audit())
    runner = WorkerRunner(handle_sigint=False)

    async def instruct(text: str) -> None:
        await call.task.queue_frames([LLMMessagesAppendFrame([{"role": "user", "content": text}], run_llm=True)])

    async def end_call() -> None:
        await call.task.queue_frame(EndFrame())  # graceful: queued speech finishes, then the call hangs up

    def ended_by_limit(reason: str, elapsed: float) -> None:
        get_audit().append(
            "call_ended_by_limit",
            {"caller_ref": caller_ref(caller_id), "call_sid": call_sid, "reason": reason, "elapsed_seconds": elapsed},
        )

    watcher = asyncio.create_task(watch_call(call.timer, instruct, end_call, ended_by_limit))
    try:
        await runner.add_workers(call.task)
        await runner.run()  # ends when the call's pipeline ends
    finally:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher
        if call.capture.active and call_sid:
            # The stream ended because the call went to Twilio <Pay>; the call isn't over.
            SUSPENDED.suspend(
                call_sid,
                messages=call.context.get_messages(),
                outcomes=call.outcomes,
                policy=call.policy,
                step_up=call.step_up,
                started_at=call.timer.started_at,
            )
            get_audit().append(
                "keypad_handoff", {"caller_ref": caller_ref(caller_id), "call_sid": call_sid, **call.capture.flags()}
            )
        else:
            # Summarize whatever was said and done, even if the call ended abruptly.
            await finalize_call(call.context.get_messages(), call.outcomes, call_sid, caller_id)
