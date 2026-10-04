# Corey Mathie, 2026
"""
The shared (Redis) velocity store behaves exactly like the in-memory one, and
unlike the in-memory one it enforces limits across instances. The same tests run
against an in-memory Redis stand-in (tests/fake_redis.py) and, when installed,
fakeredis.
"""

import json

import pytest

from src.safeguards.velocity import (
    DEFAULT_RULES,
    VelocityConfigError,
    VelocityRule,
    VelocityStore,
    velocity_store_from_env,
)
from src.safeguards.velocity_redis import UNAVAILABLE, RedisVelocityStore
from tests.fake_redis import FakeRedis

CALLER = "+15555550100"


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


def _fakeredis():
    fakeredis = pytest.importorskip("fakeredis")
    return fakeredis.FakeStrictRedis()


BACKENDS = {
    "memory": lambda clock, client=None, rules=DEFAULT_RULES: VelocityStore(rules=rules, clock=clock),
    "redis-fake": lambda clock, client=None, rules=DEFAULT_RULES: RedisVelocityStore(
        client or FakeRedis(), rules=rules, clock=clock, salt="t"
    ),
    "fakeredis": lambda clock, client=None, rules=DEFAULT_RULES: RedisVelocityStore(
        client or _fakeredis(), rules=rules, clock=clock, salt="t"
    ),
}


@pytest.fixture(params=list(BACKENDS))
def make(request):
    return BACKENDS[request.param]


# ---------- Same behavior as the in-memory store ----------


def test_payment_burst_is_denied(make):
    clock = Clock()
    store = make(clock)
    assert store.check(CALLER, "take_payment", "a") == (True, None)
    clock.now += 20
    allowed, reason = store.check(CALLER, "take_payment", "b")
    assert not allowed and "limit 1" in reason and "deny" in reason


def test_window_expires(make):
    clock = Clock()
    store = make(clock)
    store.check(CALLER, "take_payment", "a")
    clock.now += 121
    assert store.check(CALLER, "take_payment", "b")[0]


def test_replay_counts_once(make):
    clock = Clock()
    store = make(clock)
    assert store.check(CALLER, "take_payment", "same")[0]
    clock.now += 5
    assert store.check(CALLER, "take_payment", "same")[0]


def test_release_forgets_a_request_with_no_effect(make):
    clock = Clock()
    store = make(clock)
    store.check(CALLER, "take_payment", "failed")
    store.release(CALLER, "take_payment", "failed")
    clock.now += 5
    assert store.check(CALLER, "take_payment", "retry")[0]


def test_require_human_is_tagged_not_denied(make):
    clock = Clock()
    store = make(clock)
    for i in range(3):
        assert store.check(CALLER, "take_payment", f"p{i}") == (True, None)
        clock.now += 300
    allowed, reason = store.check(CALLER, "take_payment", "p3")
    assert allowed and "require_human" in reason


def test_denied_requests_still_count(make):
    clock = Clock()
    store = make(clock, rules=(VelocityRule("book_meeting", 1, 100),))
    store.check(CALLER, "book_meeting", "a")
    clock.now += 60
    assert not store.check(CALLER, "book_meeting", "b")[0]
    clock.now += 50  # "a" left the window, but the denied "b" is still in it
    assert not store.check(CALLER, "book_meeting", "c")[0]


def test_callers_and_tools_are_isolated(make):
    clock = Clock()
    store = make(clock)
    store.check(CALLER, "take_payment", "a")
    assert store.check("+15555550101", "take_payment", "b")[0]
    assert store.check(CALLER, "book_meeting", "c")[0]


def test_tools_without_rules_are_allowed(make):
    store = make(Clock())
    assert all(store.check(CALLER, "verify_nothing", f"x{i}")[0] for i in range(50))


def test_reset(make):
    clock = Clock()
    store = make(clock)
    store.check(CALLER, "take_payment", "a")
    store.reset(CALLER)
    assert store.check(CALLER, "take_payment", "b")[0]
    store.reset()
    assert store.check(CALLER, "take_payment", "c")[0]


def test_same_decisions_as_memory_over_a_long_random_sequence(make):
    import random

    rng = random.Random(7)
    clock_a, clock_b = Clock(), Clock()
    memory = VelocityStore(clock=clock_a)
    other = make(clock_b)
    tools = ["take_payment", "book_meeting", "send_verification_code", "verify_caller", "update_contact"]
    for i in range(400):
        step = rng.choice([1, 5, 30, 90, 200])
        clock_a.now += step
        clock_b.now += step
        caller = rng.choice([CALLER, "+15555550101"])
        tool = rng.choice(tools)
        rid = rng.choice([f"r{i}", f"r{max(0, i - 1)}", None])
        a = memory.check(caller, tool, rid)
        b = other.check(caller, tool, rid)
        assert a == b, (i, caller, tool, rid)
        if rid and rng.random() < 0.2:
            memory.release(caller, tool, rid)
            other.release(caller, tool, rid)


# ---------- What only the shared store does ----------


def test_limits_hold_across_instances_only_with_the_shared_store():
    clock = Clock()
    shared = FakeRedis()
    a = RedisVelocityStore(shared, clock=clock, salt="s")
    b = RedisVelocityStore(shared, clock=clock, salt="s")
    assert a.check(CALLER, "take_payment", "on-instance-a")[0]
    clock.now += 20
    assert not b.check(CALLER, "take_payment", "on-instance-b")[0]  # instance b sees instance a's request

    m1, m2 = VelocityStore(clock=clock), VelocityStore(clock=clock)
    assert m1.check(CALLER, "take_payment", "x")[0] and m2.check(CALLER, "take_payment", "y")[0]  # per process


def test_concurrent_checks_never_both_get_through():
    import threading

    clock = Clock()
    shared = FakeRedis()
    stores = [RedisVelocityStore(shared, clock=clock, salt="s") for _ in range(8)]
    results = []

    def go(i):
        results.append(stores[i].check(CALLER, "take_payment", f"c{i}")[0])

    threads = [threading.Thread(target=go, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 1


def test_no_phone_numbers_in_redis():
    shared = FakeRedis()
    RedisVelocityStore(shared, salt="s").check(CALLER, "take_payment", "a")
    dump = json.dumps(shared.zsets)
    assert "5555550100" not in dump and all(k.startswith("velocity:") for k in shared.zsets)


def test_unreachable_redis_fails_closed_by_default():
    shared = FakeRedis()
    shared.down = True
    store = RedisVelocityStore(shared, salt="s")
    assert store.check(CALLER, "take_payment", "a") == (False, UNAVAILABLE)
    store.release(CALLER, "take_payment", "a")  # best effort, doesn't raise
    assert RedisVelocityStore(shared, salt="s", fail_open=True).check(CALLER, "take_payment", "a") == (True, None)


async def test_make_handler_denies_when_the_shared_store_is_down(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from src.agent import tools
    from src.safeguards.audit_log import AuditLog
    from src.safeguards.policy_gate import CallPolicy

    monkeypatch.setenv("AUDIT_SALT", "t")
    shared = FakeRedis()
    shared.down = True
    ran = []

    async def exec_(args, idem):
        ran.append(args)
        return {"status": "booked"}

    audit = AuditLog(tmp_path / "audit.jsonl")
    h = tools.make_handler(
        "book_meeting",
        CALLER,
        {"book_meeting": exec_},
        velocity=RedisVelocityStore(shared),
        audit=audit,
        policy=CallPolicy(),
    )
    out = []

    async def cb(result, **_):
        out.append(result)

    await h(SimpleNamespace(arguments={"caller_name": "A"}, tool_call_id="t1", result_callback=cb))
    assert out[0]["status"] == "denied" and ran == []
    assert "store unavailable" in audit.path.read_text()


# ---------- Selecting the backend ----------


def test_backend_selection_from_env(monkeypatch):
    assert isinstance(velocity_store_from_env(env={}), VelocityStore)
    assert isinstance(velocity_store_from_env(env={"VELOCITY_BACKEND": "memory"}), VelocityStore)
    with pytest.raises(VelocityConfigError, match="one of"):
        velocity_store_from_env(env={"VELOCITY_BACKEND": "memcached"})
    with pytest.raises(VelocityConfigError, match="REDIS_URL"):
        velocity_store_from_env(env={"VELOCITY_BACKEND": "redis"})


def test_redis_backend_uses_the_redis_package(monkeypatch):
    import sys
    import types

    seen = {}
    fake_module = types.ModuleType("redis")

    class Redis:
        @staticmethod
        def from_url(url, **kw):
            seen.update(url=url, **kw)
            return FakeRedis()

    fake_module.Redis = Redis
    monkeypatch.setitem(sys.modules, "redis", fake_module)
    rules = (VelocityRule("take_payment", 2, 60),)
    store = velocity_store_from_env(
        rules, env={"VELOCITY_BACKEND": "redis", "REDIS_URL": "redis://cache.internal:6379/0"}
    )
    assert isinstance(store, RedisVelocityStore) and store.rules == rules
    assert seen["url"] == "redis://cache.internal:6379/0" and seen["socket_timeout"] == 0.5


def test_missing_redis_package_is_a_startup_error(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "redis", None)
    with pytest.raises(VelocityConfigError, match="redis package"):
        velocity_store_from_env(env={"VELOCITY_BACKEND": "redis", "REDIS_URL": "redis://localhost"})
