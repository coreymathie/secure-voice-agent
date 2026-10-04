# Corey Mathie, 2026
"""
A small in-memory stand-in for the redis-py client: only the sorted-set and key
commands RedisVelocityStore uses, with Redis's semantics for them (exclusive
"(" bounds, ZADD NX, MULTI/EXEC pipelines that run all-or-nothing in order).
tests/test_velocity_redis.py also runs the same tests against fakeredis when it
is installed.
"""

from __future__ import annotations

import fnmatch
import threading


def _bound(value) -> tuple[float, bool]:
    """Redis score bound -> (number, exclusive)."""
    if isinstance(value, str):
        if value in ("-inf", "+inf", "inf"):
            return float(value), False
        if value.startswith("("):
            return float(value[1:]), True
        return float(value), False
    return float(value), False


def _in_range(score: float, lo, hi) -> bool:
    lo_v, lo_x = _bound(lo)
    hi_v, hi_x = _bound(hi)
    above = score > lo_v if lo_x else score >= lo_v
    below = score < hi_v if hi_x else score <= hi_v
    return above and below


class FakeRedis:
    def __init__(self) -> None:
        self.zsets: dict[str, dict[str, float]] = {}
        self.ttl: dict[str, int] = {}
        self.lock = threading.Lock()
        self.down = False
        self.commands: list[str] = []

    def _check(self) -> None:
        if self.down:
            raise ConnectionError("Error 111 connecting to redis:6379. Connection refused.")

    # -- commands --

    def zremrangebyscore(self, key, lo, hi) -> int:
        z = self.zsets.get(key, {})
        gone = [m for m, s in z.items() if _in_range(s, lo, hi)]
        for m in gone:
            del z[m]
        return len(gone)

    def zadd(self, key, mapping: dict, nx: bool = False) -> int:
        z = self.zsets.setdefault(key, {})
        added = 0
        for member, score in mapping.items():
            if nx and member in z:
                continue
            added += member not in z
            z[member] = float(score)
        return added

    def zcount(self, key, lo, hi) -> int:
        return sum(1 for s in self.zsets.get(key, {}).values() if _in_range(s, lo, hi))

    def expire(self, key, seconds: int) -> bool:
        self.ttl[key] = int(seconds)
        return key in self.zsets

    def zrem(self, key, *members) -> int:
        self._check()
        z = self.zsets.get(key, {})
        return sum(1 for m in members if z.pop(m, None) is not None)

    def scan_iter(self, match: str = "*"):
        self._check()
        return [k for k in list(self.zsets) if fnmatch.fnmatchcase(k, match)]

    def delete(self, *keys) -> int:
        self._check()
        return sum(1 for k in keys if self.zsets.pop(k, None) is not None)

    def pipeline(self, transaction: bool = True) -> FakePipeline:
        return FakePipeline(self)


class FakePipeline:
    def __init__(self, redis: FakeRedis):
        self.redis = redis
        self.queued: list[tuple[str, tuple, dict]] = []

    def __getattr__(self, name):
        def queue(*args, **kwargs):
            self.queued.append((name, args, kwargs))
            return self

        return queue

    def execute(self) -> list:
        self.redis._check()
        with self.redis.lock:  # MULTI/EXEC: nothing interleaves
            self.redis.commands.extend(name for name, _, _ in self.queued)
            return [getattr(self.redis, name)(*a, **kw) for name, a, kw in self.queued]
