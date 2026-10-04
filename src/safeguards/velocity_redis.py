# Corey Mathie, 2026
"""
Redis-backed velocity store, for running more than one voice-process instance.

VelocityStore (velocity.py) keeps its sliding windows in process memory, so with
N instances a caller whose calls land on different instances gets up to N times
the limit. RedisVelocityStore keeps the same windows in Redis sorted sets, so
every instance sees every request. Same interface and the same counting rules:

  - every distinct request counts, including denied ones;
  - a replay of the same request id counts once (ZADD NX on the request id);
  - release() removes a request that had no effect (ZREM).

Each check runs as one MULTI/EXEC transaction: trim entries older than the
longest window for the tool, add this request, count each rule's window, refresh
the key's expiry. The transaction is atomic, so concurrent checks from different
instances are serialized and each one counts the others.

Keys are `<prefix>:<caller hash>:<tool>`: the caller's number is stored only as
a salted SHA-256 (the same AUDIT_SALT as the audit log), never in the clear.
Scores are wall-clock seconds (time.time), because monotonic clocks aren't
comparable across hosts; keep instance clocks in sync (NTP). Skew between hosts
shifts window edges by the skew.

Fail closed: if Redis can't be reached, check() denies the request (and says so
in the reason the audit log records) instead of letting it through unlimited.
Set fail_open=True only if availability matters more than the limits.

Selected with VELOCITY_BACKEND=redis and REDIS_URL (velocity.velocity_store_from_env).
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
import uuid
from collections.abc import Callable
from typing import Any

from .velocity import DEFAULT_RULES, VelocityRule

log = logging.getLogger(__name__)
UNAVAILABLE = "velocity: store unavailable — action: deny"


class RedisVelocityStore:
    def __init__(
        self,
        client: Any,
        rules: tuple[VelocityRule, ...] = DEFAULT_RULES,
        clock: Callable[[], float] = time.time,
        *,
        prefix: str = "velocity",
        salt: str | None = None,
        fail_open: bool = False,
    ):
        self.client = client
        self.rules = rules
        self.clock = clock
        self.prefix = prefix
        self.salt = os.environ.get("AUDIT_SALT", "change-me") if salt is None else salt
        self.fail_open = fail_open

    def _key(self, caller_id: str, tool: str) -> str:
        ref = hashlib.sha256(f"{self.salt}:{caller_id}".encode()).hexdigest()[:16]
        return f"{self.prefix}:{ref}:{tool}"

    def check(self, caller_id: str, tool: str, request_id: str | None = None) -> tuple[bool, str | None]:
        rules = [r for r in self.rules if r.tool == tool]
        if not rules:
            return True, None
        now = self.clock()
        longest = max(r.window_seconds for r in rules)
        key = self._key(caller_id, tool)
        member = request_id or f"anon:{uuid.uuid4().hex}"
        try:
            pipe = self.client.pipeline(transaction=True)
            pipe.zremrangebyscore(key, "-inf", f"({now - longest}")
            pipe.zadd(key, {member: now}, nx=True)
            for r in rules:
                pipe.zcount(key, now - r.window_seconds, "+inf")
            pipe.expire(key, int(longest) + 60)
            replies = pipe.execute()
        except Exception as e:  # noqa: BLE001 - any client/network error
            log.error("velocity store unavailable: %s", e)
            if self.fail_open:
                return True, None
            return False, UNAVAILABLE
        counts = replies[2 : 2 + len(rules)]

        highest_action: str | None = None
        reason: str | None = None
        for rule, count in zip(rules, counts, strict=True):
            if int(count) > rule.max_count:
                reason = (
                    f"velocity: {int(count)} {tool} calls in "
                    f"{rule.window_seconds}s (limit {rule.max_count}) — action: {rule.action}"
                )
                if rule.action == "deny":
                    return False, reason
                highest_action = rule.action
        return True, reason if highest_action else None

    def release(self, caller_id: str, tool: str, request_id: str) -> None:
        try:
            self.client.zrem(self._key(caller_id, tool), request_id)
        except Exception as e:  # noqa: BLE001 - best effort, like the in-memory store
            log.error("could not release velocity entry: %s", e)

    def reset(self, caller_id: str | None = None) -> None:
        pattern = f"{self.prefix}:*" if caller_id is None else self._key(caller_id, "*")
        keys = list(self.client.scan_iter(match=pattern))
        if keys:
            self.client.delete(*keys)
