# Corey Mathie, 2026
"""
Carry a call's state across the Twilio <Pay> hop.

Keypad capture redirects the call away from the agent's media stream; when <Pay>
finishes, Twilio opens a *new* stream for the same call SID. Without help the
new pipeline would start from nothing: an empty conversation, fresh per-call
caps (so a caller could reset the payment ceiling), and no step-up verification.

SuspendedCalls keeps the outgoing call's context messages, tool outcomes,
CallPolicy, and StepUpSession in memory, keyed by call SID, for a few minutes.
The resumed pipeline takes them back, so caps, verification, the risk score,
and the conversation continue, and one post-call summary covers the whole call.

Limits (documented in docs/pci.md): the store is per process. If the resumed
stream lands on a different instance, or the process restarted, the state is
gone; the new pipeline starts fresh and the audit log records
`resume_state_missing`. A shared store would close that gap (not implemented).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

DEFAULT_TTL_SECONDS = 15 * 60  # long enough for a keypad payment, short enough not to pile up


@dataclass
class SuspendedCall:
    messages: list
    outcomes: list[dict]
    policy: Any  # CallPolicy
    step_up: Any  # StepUpSession
    suspended_at: float
    started_at: float | None = None  # when the call began, so the max-duration limit spans the <Pay> hop


@dataclass
class SuspendedCalls:
    ttl_seconds: float = DEFAULT_TTL_SECONDS
    clock: Callable[[], float] = time.monotonic
    _calls: dict[str, SuspendedCall] = field(default_factory=dict)

    def suspend(
        self,
        call_sid: str,
        *,
        messages: list,
        outcomes: list[dict],
        policy: Any,
        step_up: Any,
        started_at: float | None = None,
    ) -> None:
        self._expire()
        self._calls[call_sid] = SuspendedCall(
            list(messages), list(outcomes), policy, step_up, self.clock(), started_at=started_at
        )

    def resume(self, call_sid: str | None) -> SuspendedCall | None:
        self._expire()
        return self._calls.pop(call_sid, None) if call_sid else None

    def __contains__(self, call_sid: str) -> bool:
        self._expire()
        return call_sid in self._calls

    def _expire(self) -> None:
        cutoff = self.clock() - self.ttl_seconds
        for sid in [s for s, c in self._calls.items() if c.suspended_at < cutoff]:
            del self._calls[sid]


SUSPENDED = SuspendedCalls()
