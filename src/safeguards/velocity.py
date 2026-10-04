# Corey Mathie, 2026
"""
Velocity checks for voice-agent tool calls.

Pattern applied from fintech dispute/fraud work: when a single caller
produces an unusual burst of state-changing requests ("book 5 meetings
in 90 seconds", "charge 3 cards in a row"), that's a strong fraud or
confused-agent signal. We throttle and escalate rather than execute.

Rules are declarative — see `DEFAULT_RULES` (and config/policy.yaml, which
production loads). Keyed on `(caller_id, tool)` with sliding windows. This
store keeps them in process memory; for more than one voice-process instance
use RedisVelocityStore (velocity_redis.py, same interface), selected with
VELOCITY_BACKEND=redis by velocity_store_from_env().
"""

from __future__ import annotations

import os
import time
from collections import defaultdict, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class VelocityRule:
    tool: str
    max_count: int
    window_seconds: int
    action: str = "deny"  # "deny" | "flag" | "require_human"


DEFAULT_RULES: tuple[VelocityRule, ...] = (
    VelocityRule(tool="take_payment", max_count=1, window_seconds=120),
    VelocityRule(tool="take_payment", max_count=3, window_seconds=3600, action="require_human"),
    VelocityRule(tool="book_meeting", max_count=3, window_seconds=180),
    VelocityRule(tool="create_ticket", max_count=5, window_seconds=600),
    VelocityRule(tool="log_lead", max_count=5, window_seconds=600),
    # Step-up verification: codes cost money and can be abused for SMS pumping; guesses are capped across calls.
    VelocityRule(tool="send_verification_code", max_count=3, window_seconds=600),
    VelocityRule(tool="verify_caller", max_count=6, window_seconds=3600),
    # A second contact change from the same number within a day goes to a person.
    VelocityRule(tool="update_contact", max_count=1, window_seconds=86400, action="require_human"),
)


class VelocityStore:
    """
    Sliding-window counters per (caller, tool).

    Counting rules:
      - Every distinct request counts, including ones that get denied, so a caller
        who keeps hammering stays blocked.
      - A replay of the same request id (a network retry of one tool call) is not
        counted twice.
      - `release()` removes a request that had no effect (rejected by validation,
        or failed upstream), so an honest retry isn't punished for our outage.
    """

    def __init__(self, rules: tuple[VelocityRule, ...] = DEFAULT_RULES, clock: Callable[[], float] = time.monotonic):
        self.rules = rules
        self.clock = clock
        self._events: dict[tuple[str, str], deque[tuple[float, str | None]]] = defaultdict(deque)

    def check(self, caller_id: str, tool: str, request_id: str | None = None) -> tuple[bool, str | None]:
        """
        Register that `caller_id` just invoked `tool`. Return (allowed, reason).

        If any rule fires with action="deny", reject. If any rule fires with
        "require_human", allow but tag — the agent should hand off to a human.
        """
        now = self.clock()
        q = self._events[(caller_id, tool)]
        # Trim anything older than the longest window for this tool so memory stays bounded.
        longest = max((r.window_seconds for r in self.rules if r.tool == tool), default=0)
        while q and q[0][0] < now - longest:
            q.popleft()
        if request_id is None or all(rid != request_id for _, rid in q):
            q.append((now, request_id))

        highest_action: str | None = None
        reason: str | None = None
        for rule in self.rules:
            if rule.tool != tool:
                continue
            cutoff = now - rule.window_seconds
            count_in_window = sum(1 for t, _ in q if t >= cutoff)
            if count_in_window > rule.max_count:
                reason = (
                    f"velocity: {count_in_window} {tool} calls in "
                    f"{rule.window_seconds}s (limit {rule.max_count}) — action: {rule.action}"
                )
                if rule.action == "deny":
                    return False, reason
                highest_action = rule.action

        return True, reason if highest_action else None

    def release(self, caller_id: str, tool: str, request_id: str) -> None:
        """Forget a request that didn't do anything, so it doesn't count toward limits."""
        q = self._events.get((caller_id, tool))
        if q:
            self._events[(caller_id, tool)] = deque(e for e in q if e[1] != request_id)

    def reset(self, caller_id: str | None = None) -> None:
        if caller_id is None:
            self._events.clear()
            return
        for key in [k for k in self._events if k[0] == caller_id]:
            del self._events[key]


VELOCITY_BACKENDS = ("memory", "redis")


class VelocityConfigError(Exception):
    """VELOCITY_BACKEND names something unusable (unknown backend, redis without REDIS_URL or the package)."""


def velocity_store_from_env(rules: tuple[VelocityRule, ...] = DEFAULT_RULES, env: Mapping[str, str] | None = None):
    """
    VELOCITY_BACKEND=memory (default): this process only.
    VELOCITY_BACKEND=redis: shared by every instance (REDIS_URL, optional VELOCITY_REDIS_PREFIX).
    A misconfigured backend raises at startup rather than silently falling back to memory.
    """
    env = os.environ if env is None else env
    backend = (env.get("VELOCITY_BACKEND") or "memory").strip().lower()
    if backend == "memory":
        return VelocityStore(rules=rules)
    if backend != "redis":
        raise VelocityConfigError(f"VELOCITY_BACKEND must be one of {VELOCITY_BACKENDS}, not {backend!r}")
    url = (env.get("REDIS_URL") or "").strip()
    if not url:
        raise VelocityConfigError("VELOCITY_BACKEND=redis needs REDIS_URL")
    try:
        import redis
    except ImportError as e:
        raise VelocityConfigError("VELOCITY_BACKEND=redis needs the redis package (pip install redis)") from e
    from .velocity_redis import RedisVelocityStore

    client = redis.Redis.from_url(url, socket_timeout=0.5, socket_connect_timeout=0.5)
    return RedisVelocityStore(client, rules=rules, prefix=env.get("VELOCITY_REDIS_PREFIX", "velocity"))
