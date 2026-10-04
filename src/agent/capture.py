# Corey Mathie, 2026
"""
Keypad-capture state for one call, and a pipeline guard that enforces it.

When a keypad payment starts (take_payment returns `keypad_started` in
PAYMENT_MODE=keypad), the call is redirected to Twilio <Pay> and the media
stream to the agent ends. CaptureState records that, with two flags the rest of
the process honors while it is set:

  transcript_suppressed  the guard drops caller audio and transcription frames,
                         so nothing said or keyed during capture can reach the
                         model, the context, or the post-call summary
  recording_paused       reported by the keypad_payment Lambda (Twilio.CURRENT
                         recording paused before <Pay> starts)

The guard (make_capture_guard) also drops keypad tones (InputDTMFFrame) at all
times: nothing in this agent reads DTMF from the media stream, so card digits a
caller types outside <Pay> never reach the pipeline either.

Because the stream really does end during <Pay>, the guard is defense in depth:
it matters if a deployment changes the flow (for example keeps the stream open).

CaptureState is plain Python (the browser demo uses it); the guard imports
Pipecat lazily.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class CaptureState:
    clock: Callable[[], float] = time.monotonic
    active: bool = False
    transcript_suppressed: bool = False
    recording_paused: bool = False
    started_at: float | None = None
    dropped_frames: int = 0
    dropped_dtmf: int = 0
    history: list[dict] = field(default_factory=list)

    def begin(self, recording: str | None = None) -> None:
        self.active = True
        self.transcript_suppressed = True
        self.recording_paused = recording == "paused"
        self.started_at = self.clock()
        self.history.append({"event": "capture_started", "recording": recording or "unknown"})

    def end(self, result: str) -> None:
        self.active = False
        self.transcript_suppressed = False
        self.recording_paused = False
        self.history.append({"event": "capture_ended", "result": result})

    def flags(self) -> dict:
        return {
            "active": self.active,
            "transcript_suppressed": self.transcript_suppressed,
            "recording_paused": self.recording_paused,
        }


def make_capture_guard(state: CaptureState):
    """A Pipecat FrameProcessor placed right after transport.input()."""
    from pipecat.frames.frames import (
        InputAudioRawFrame,
        InputDTMFFrame,
        InterimTranscriptionFrame,
        TranscriptionFrame,
    )
    from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

    suppressible = (InputAudioRawFrame, TranscriptionFrame, InterimTranscriptionFrame)

    class CaptureGuard(FrameProcessor):
        async def process_frame(self, frame, direction: FrameDirection):
            await super().process_frame(frame, direction)
            if isinstance(frame, InputDTMFFrame):
                state.dropped_dtmf += 1
                return
            if state.transcript_suppressed and isinstance(frame, suppressible):
                state.dropped_frames += 1
                return
            await self.push_frame(frame, direction)

    return CaptureGuard(name="capture-guard")
