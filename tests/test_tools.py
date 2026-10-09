# Corey Mathie, 2026
"""Tests for the safeguarded tool handlers the LLM invokes."""

import json
from types import SimpleNamespace

import pytest

from src.agent import tools


class FakeParams(SimpleNamespace):
    """Stand-in for Pipecat's FunctionCallParams."""


def _params(args: dict, tool_call_id: str = "call_1"):
    results = []

    async def result_callback(result, **_):
        results.append(result)

    return FakeParams(arguments=args, tool_call_id=tool_call_id, result_callback=result_callback), results


@pytest.fixture(autouse=True)
def fresh_state(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("AUDIT_SALT", "test-salt")
    monkeypatch.setattr(tools, "_audit", None)
    tools._velocity.reset()
    yield tmp_path / "audit.jsonl"


def _verified():
    """A call whose caller already passed step-up verification (high-tier tools need it)."""
    from src.safeguards.policy_gate import CallPolicy

    return CallPolicy(verified=True)


def _audit_rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_specs_cover_every_executor():
    # Lambda-backed tools have an HTTPS executor; step-up tools are bound per call (tools.step_up_executors).
    assert set(tools.TOOL_NAMES) == set(tools.EXECUTORS) | set(tools.STEP_UP_TOOLS)
    assert not set(tools.EXECUTORS) & set(tools.STEP_UP_TOOLS)
    for spec in tools.TOOL_SPECS:
        assert set(spec["required"]) <= set(spec["properties"])


async def test_handler_redacts_audits_and_returns_result(fresh_state):
    calls = []

    async def fake_exec(args, idem):
        calls.append((args, idem))
        return {"status": "booked"}

    handler = tools.make_handler("book_meeting", "+15555550100", executors={"book_meeting": fake_exec})
    params, results = _params(
        {
            "caller_name": "Alice",
            "caller_email": "alice@example.com",
            "start_iso": "2026-10-06T14:00:00",
            "topic": "SSN 123-45-6789 question",
        }
    )
    await handler(params)

    assert results == [{"status": "booked"}]
    assert calls[0][1] == "call_1"  # LLM tool_call_id is the idempotency key
    raw = fresh_state.read_text()
    assert "123-45-6789" not in raw and "alice@example.com" not in raw  # PII never hits the log
    assert "+15555550100" not in raw  # caller number stored only as a salted hash
    events = [r["event"] for r in _audit_rows(fresh_state)]
    assert events == ["tool_call", "tool_result"]


async def test_payment_link_is_sent_to_caller_number():
    seen = {}

    async def fake_exec(args, idem):
        seen.update(args)
        return {"status": "link_sent"}

    handler = tools.make_handler(
        "take_payment", "+15555550100", executors={"take_payment": fake_exec}, policy=_verified()
    )
    params, _ = _params({"amount_usd": 40, "description": "Credit card payment", "customer_email": "a@b.co"})
    await handler(params)
    assert seen["customer_phone"] == "+15555550100"


async def test_velocity_denies_second_rapid_payment(fresh_state):
    async def fake_exec(args, idem):
        return {"status": "link_sent"}

    handler = tools.make_handler(
        "take_payment", "+15550001111", executors={"take_payment": fake_exec}, policy=_verified()
    )
    p1, r1 = _params({"amount_usd": 10, "description": "x", "customer_email": "a@b.co"}, "c1")
    p2, r2 = _params({"amount_usd": 10, "description": "x", "customer_email": "a@b.co"}, "c2")
    await handler(p1)
    await handler(p2)
    assert r1[0]["status"] == "link_sent"
    assert r2[0]["status"] == "denied"
    assert "velocity_denied" in [r["event"] for r in _audit_rows(fresh_state)]


async def test_executor_failure_returns_safe_message(fresh_state):
    async def boom(args, idem):
        raise RuntimeError("upstream 500 with internal details")

    handler = tools.make_handler("create_ticket", "+15550002222", executors={"create_ticket": boom})
    params, results = _params({"subject": "s", "body": "b", "caller_email": "a@b.co"})
    await handler(params)
    assert results[0]["status"] == "error"
    assert "internal details" not in json.dumps(results[0])
    assert _audit_rows(fresh_state)[-1]["event"] == "tool_error"
