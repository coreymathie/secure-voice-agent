# Corey Mathie, 2026
"""
Maximum call duration and idle timeout, with a graceful goodbye.

A phone line that never hangs up costs money (telephony, model minutes) and
keeps a worker busy; a caller who walked away leaves the agent talking to
nobody. CallTimer is the clock for both:

  idle       no caller speech for CALL_IDLE_PROMPT_SECONDS -> the agent asks if
             they're still there; for CALL_IDLE_TIMEOUT_SECONDS -> goodbye and
             hang up. Silence is measured from the end of the agent's own
             speech, so a long answer doesn't count against the caller.
  duration   CALL_DURATION_WARNING_SECONDS before CALL_MAX_DURATION_SECONDS the
             agent says the call needs to wrap up; at the maximum -> goodbye
             and hang up. Measured from the start of the call, including any
             time spent in a keypad payment.

watch_call() polls the timer and, at a limit: audits `call_ended_by_limit`
(reason and elapsed seconds), asks the model to say a short goodbye, waits
`goodbye_grace_seconds` for it to be spoken, then ends the pipeline (EndFrame,
which lets queued audio finish; with Twilio credentials set, ending the
pipeline hangs up the call). Pipecat's own idle timeout on the PipelineWorker
(300 s by default, abrupt cancel) stays in place as a backstop.

Instructions to the model start with CONTROL_PREFIX, so the transcript, the
risk scorer, and the post-call summary don't mistake them for the caller.

A value of 0 turns a limit off. The timer and watcher are plain Python with an
injected clock and sleep, so tests run them without Pipecat or real time.
"""

from __future__ import annotations

import asyncio
import math
import os
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

CONTROL_PREFIX = "[call control]"

INSTRUCTIONS = {
    "idle_prompt": f"{CONTROL_PREFIX} The caller has been quiet for a while. Briefly ask if they're still there.",
    "duration_warning": (
        f"{CONTROL_PREFIX} This call is close to its time limit. Tell the caller you'll need to wrap up in about "
        "a minute, and offer to have a team member follow up on anything left."
    ),
    "idle": (
        f"{CONTROL_PREFIX} The caller hasn't responded. Say a short, polite goodbye now, for example: \"I haven't "
        "heard from you, so I'll end the call. Feel free to call back any time. Goodbye.\" Don't ask anything else "
        "and don't call any tools."
    ),
    "max_duration": (
        f"{CONTROL_PREFIX} The call has reached its time limit. Say a short, polite goodbye now and mention that a "
        "team member can follow up. Don't start anything new and don't call any tools."
    ),
}


@dataclass(frozen=True)
class CallLimits:
    max_duration_seconds: float = 900.0
    duration_warning_seconds: float = 60.0
    idle_timeout_seconds: float = 30.0
    idle_prompt_seconds: float = 15.0
    goodbye_grace_seconds: float = 8.0
    poll_seconds: float = 1.0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> CallLimits:
        """CALL_* variables; malformed or negative values keep the default, 0 turns a limit off."""
        env = os.environ if env is None else env
        d = cls()

        def num(key: str, default: float) -> float:
            try:
                value = float(env[key])
            except (KeyError, TypeError, ValueError):
                return default
            return value if math.isfinite(value) and value >= 0 else default

        return cls(
            max_duration_seconds=num("CALL_MAX_DURATION_SECONDS", d.max_duration_seconds),
            duration_warning_seconds=num("CALL_DURATION_WARNING_SECONDS", d.duration_warning_seconds),
            idle_timeout_seconds=num("CALL_IDLE_TIMEOUT_SECONDS", d.idle_timeout_seconds),
            idle_prompt_seconds=num("CALL_IDLE_PROMPT_SECONDS", d.idle_prompt_seconds),
            goodbye_grace_seconds=num("CALL_GOODBYE_GRACE_SECONDS", d.goodbye_grace_seconds),
            poll_seconds=max(0.05, num("CALL_LIMIT_POLL_SECONDS", d.poll_seconds)),
        )


class CallTimer:
    def __init__(
        self,
        limits: CallLimits | None = None,
        clock: Callable[[], float] = time.monotonic,
        started_at: float | None = None,
    ):
        self.limits = limits or CallLimits()
        self.clock = clock
        self.started_at = clock() if started_at is None else started_at
        self.last_activity = self.clock()
        self.bot_speaking = False
        self.ended: str | None = None
        self._warned = False
        self._prompted = False

    def elapsed(self) -> float:
        return self.clock() - self.started_at

    def caller_activity(self) -> None:
        self.last_activity = self.clock()
        self._prompted = False

    def set_bot_speaking(self, speaking: bool) -> None:
        self.bot_speaking = speaking
        if not speaking:
            self.last_activity = self.clock()  # silence counts from the end of the agent's turn

    def poll(self) -> list[str]:
        """Actions due now: idle_prompt, duration_warning, end:idle, end:max_duration. Each fires once."""
        if self.ended:
            return []
        lim = self.limits
        now = self.clock()
        actions: list[str] = []
        if lim.max_duration_seconds > 0:
            if now - self.started_at >= lim.max_duration_seconds:
                self.ended = "max_duration"
                return ["end:max_duration"]
            warn_at = lim.max_duration_seconds - lim.duration_warning_seconds
            if lim.duration_warning_seconds > 0 and not self._warned and now - self.started_at >= warn_at:
                self._warned = True
                actions.append("duration_warning")
        if lim.idle_timeout_seconds > 0 and not self.bot_speaking:
            silent = now - self.last_activity
            if silent >= lim.idle_timeout_seconds:
                self.ended = "idle"
                return [*actions, "end:idle"]
            if 0 < lim.idle_prompt_seconds <= silent and not self._prompted:
                self._prompted = True
                actions.append("idle_prompt")
        return actions


async def watch_call(
    timer: CallTimer,
    instruct: Callable[[str], Awaitable[None]],
    end_call: Callable[[], Awaitable[None]],
    on_end: Callable[[str, float], None],
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> str:
    """Run until a limit ends the call; returns the reason ("idle" or "max_duration")."""
    while True:
        await sleep(timer.limits.poll_seconds)
        for action in timer.poll():
            if not action.startswith("end:"):
                await instruct(INSTRUCTIONS[action])
                continue
            reason = action.removeprefix("end:")
            on_end(reason, round(timer.elapsed(), 1))
            await instruct(INSTRUCTIONS[reason])
            await sleep(timer.limits.goodbye_grace_seconds)
            await end_call()
            return reason


def make_activity_observer(timer: CallTimer):
    """A Pipecat observer that feeds caller speech and the agent's speaking state into the timer."""
    from pipecat.frames.frames import (
        BotStartedSpeakingFrame,
        BotStoppedSpeakingFrame,
        InterimTranscriptionFrame,
        TranscriptionFrame,
        UserStartedSpeakingFrame,
        UserStoppedSpeakingFrame,
    )
    from pipecat.observers.base_observer import BaseObserver

    caller = (UserStartedSpeakingFrame, UserStoppedSpeakingFrame, TranscriptionFrame, InterimTranscriptionFrame)

    class CallActivityObserver(BaseObserver):
        async def on_push_frame(self, data) -> None:
            frame = data.frame
            if isinstance(frame, caller):
                timer.caller_activity()
            elif isinstance(frame, BotStartedSpeakingFrame):
                timer.set_bot_speaking(True)
            elif isinstance(frame, BotStoppedSpeakingFrame):
                timer.set_bot_speaking(False)

    return CallActivityObserver()
