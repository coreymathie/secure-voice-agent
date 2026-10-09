# Corey Mathie, 2026
"""Post-call summaries: transcript extraction, both model backends, fallbacks, delivery, and the CRM Lambda."""

import json
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from src.agent import summary
from src.safeguards.audit_log import AuditLog

MESSAGES = [
    {"role": "user", "content": summary.KICKOFF_MESSAGE},
    {"role": "assistant", "content": "Thanks for calling Cypress Harbor Credit Union, how can I help?"},
    {
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": "I need a loan officer appointment about a home equity line. Email is dana@example.com",
            }
        ],
    },
    {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "book_meeting", "arguments": "{}"}}],
    },
    {"role": "tool", "tool_call_id": "c1", "content": '{"status": "booked"}'},
    {"role": "assistant", "content": "You're booked for Tuesday at 2pm."},
    {"role": "user", "content": "Great. My SSN is 123-45-6789 if you need it."},
    SimpleNamespace(llm="openai", message={"type": "provider-specific"}),
]
OUTCOMES = [{"tool": "book_meeting", "status": "booked"}]
GOOD = {
    "summary": "Dana called about a home equity line and was booked with a loan officer for Tuesday at 2pm.",
    "caller_intent": "home equity line appointment",
    "outcome": "resolved",
    "follow_ups": ["Prepare home equity application materials"],
    "sentiment": "positive",
}


@pytest.fixture(autouse=True)
def no_keys(monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "SUMMARY_PROVIDER", "SUMMARY_MODEL", "LAMBDA_BASE_URL"):
        monkeypatch.delenv(k, raising=False)


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_transcript_skips_kickoff_tools_and_provider_messages():
    turns = summary.transcript_from_messages(MESSAGES)
    assert [r for r, _ in turns] == ["assistant", "user", "assistant", "user"]
    assert turns[1][1].startswith("I need a loan officer appointment")


async def test_anthropic_summary_is_forced_through_a_tool_and_sees_only_redacted_text(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    sent = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.content))
        assert request.headers["x-api-key"] == "sk-ant-test"
        block = {"type": "tool_use", "name": "record_call_summary", "input": GOOD}
        return httpx.Response(200, json={"content": [block]})

    async with _client(handler) as client:
        result = await summary.summarize(summary.transcript_from_messages(MESSAGES), OUTCOMES, client)

    assert result.outcome == "resolved" and result.generated_by == "anthropic:claude-haiku-4-5"
    assert sent["tool_choice"] == {"type": "tool", "name": "record_call_summary"}
    prompt = sent["messages"][0]["content"]
    assert "book_meeting: booked" in prompt
    assert "123-45-6789" not in prompt and "dana@example.com" not in prompt


async def test_openai_summary_uses_strict_json_schema(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    sent = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(GOOD)}}]})

    async with _client(handler) as client:
        result = await summary.summarize(summary.transcript_from_messages(MESSAGES), OUTCOMES, client)
    assert result.generated_by == "openai:gpt-4.1-mini" and result.follow_ups == [
        "Prepare home equity application materials"
    ]
    assert sent["response_format"]["json_schema"]["strict"] is True


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, json={"error": "overloaded"}),
        httpx.Response(200, json={"content": [{"type": "tool_use", "input": {**GOOD, "outcome": "great"}}]}),
        httpx.Response(200, json={"content": [{"type": "text", "text": "no tool call"}]}),
    ],
)
async def test_bad_model_output_falls_back_to_rules(monkeypatch, response):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    async with _client(lambda request: response) as client:
        result = await summary.summarize(summary.transcript_from_messages(MESSAGES), OUTCOMES, client)
    assert result.generated_by == "rules" and result.outcome == "resolved"


@pytest.mark.parametrize(
    ("outcomes", "turns", "expected"),
    [
        ([{"tool": "take_payment", "status": "require_human"}], 3, "escalated"),
        ([{"tool": "create_ticket", "status": "error"}], 3, "follow_up"),
        ([{"tool": "take_payment", "status": "error"}, {"tool": "take_payment", "status": "link_sent"}], 3, "resolved"),
        ([], 1, "abandoned"),
        ([], 4, "follow_up"),
    ],
)
def test_rule_based_outcomes(outcomes, turns, expected):
    t = [("user", "hi there")] * turns
    assert summary.rule_based(t, outcomes).outcome == expected


async def test_deliver_audits_and_posts_once_per_call(tmp_path, monkeypatch):
    monkeypatch.setenv("LAMBDA_BASE_URL", "https://api.example.com/prod")
    audit = AuditLog(tmp_path / "audit.jsonl")
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"status": "saved"})

    s = summary.CallSummary(**GOOD, tools=OUTCOMES, caller_turns=2, generated_by="rules")
    async with _client(handler) as client:
        out = await summary.deliver(s, "CA123", "+19545550101", "ref123", audit, client)

    assert out == {"status": "saved"}
    req = seen[0]
    assert req.url.path == "/prod/call_summary" and req.headers["Idempotency-Key"] == "summary-CA123"
    assert json.loads(req.content)["caller_phone"] == "+19545550101"
    raw = (tmp_path / "audit.jsonl").read_text()
    assert '"call_summary"' in raw and "+19545550101" not in raw


async def test_delivery_failure_is_recorded_not_raised(tmp_path, monkeypatch):
    monkeypatch.setenv("LAMBDA_BASE_URL", "https://api.example.com/prod")
    audit = AuditLog(tmp_path / "audit.jsonl")
    s = summary.rule_based([("user", "hi")], [])
    async with _client(lambda r: httpx.Response(503)) as client:
        assert await summary.deliver(s, "CA9", "+1555", "ref", audit, client) is None
    assert "call_summary_error" in (tmp_path / "audit.jsonl").read_text()


async def test_finalize_never_raises(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("model exploded")

    monkeypatch.setattr(summary, "summarize", boom)
    await summary.finalize_call(MESSAGES, OUTCOMES, "CA1", "+1555")  # no exception


# ---------- call_summary Lambda ----------


def _event(body: dict, key: str = "summary-CA1"):
    return {"body": json.dumps(body), "headers": {"Idempotency-Key": key}}


BODY = {
    "call_sid": "CA1",
    "caller_phone": "+19545550101",
    **GOOD,
    "summary": "Caller read card 4111 1111 1111 1111 aloud; booked a loan officer appointment.",
    "tools": OUTCOMES,
}


def test_lambda_upserts_contact_and_adds_scrubbed_note(monkeypatch):
    from src.handlers import call_summary

    monkeypatch.setenv("GOHIGHLEVEL_API_KEY", "ghl")
    monkeypatch.setenv("GOHIGHLEVEL_LOCATION_ID", "loc1")
    calls = []

    def ghl(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, json.loads(request.content)))
        if request.url.path == "/contacts/upsert":
            return httpx.Response(200, json={"contact": {"id": "ct_9"}})
        return httpx.Response(201, json={"note": {"id": "nt_1"}})

    fake = SimpleNamespace(Client=lambda **kw: httpx.Client(transport=httpx.MockTransport(ghl), **kw))
    with patch.object(call_summary, "httpx", fake), patch("src.handlers._common.seen_before", return_value=False):
        resp = call_summary.handler(_event(BODY), None)

    assert json.loads(resp["body"]) == {"status": "saved", "contact_id": "ct_9", "note_id": "nt_1"}
    assert calls[0] == ("/contacts/upsert", {"locationId": "loc1", "phone": "+19545550101", "source": "voice-agent"})
    note = calls[1][1]["body"]
    assert calls[1][0] == "/contacts/ct_9/notes" and "Resolved" in note and "book_meeting → booked" in note
    assert "4111 1111 1111 1111" not in note


def test_lambda_without_crm_skips_cleanly(monkeypatch):
    from src.handlers import call_summary

    monkeypatch.delenv("GOHIGHLEVEL_API_KEY", raising=False)
    with patch("src.handlers._common.seen_before", return_value=False):
        resp = call_summary.handler(_event(BODY), None)
    assert json.loads(resp["body"])["status"] == "skipped"


def test_lambda_validates_and_releases_key():
    from src.handlers import call_summary

    with patch("src.handlers._common.seen_before", return_value=False), patch("src.handlers._common.release") as rel:
        resp = call_summary.handler(_event({"call_sid": "CA1"}), None)
    assert resp["statusCode"] == 400 and rel.called
