# Corey Mathie, 2026
"""Lambda handler tests. No network: DynamoDB, Stripe, and Google are stubbed."""

import base64
import json
from unittest.mock import patch

import pytest


def _event(body: dict, idempotency_key: str = "test-key"):
    return {"body": json.dumps(body), "headers": {"Idempotency-Key": idempotency_key}}


@pytest.fixture
def no_dynamo():
    """Pretend every key is new and swallow releases."""
    with patch("src.handlers._common.seen_before", return_value=False), patch("src.handlers._common.release") as rel:
        yield rel


# ---------- Twilio webhook ----------


def _twilio_event(form: str, signature: str = ""):
    return {
        "body": base64.b64encode(form.encode()).decode(),
        "isBase64Encoded": True,
        "headers": {"X-Twilio-Signature": signature},
        "rawPath": "/voice",
        "requestContext": {"domainName": "abc.execute-api.us-east-1.amazonaws.com"},
    }


def test_voice_hook_returns_stream_twiml_with_caller(monkeypatch):
    monkeypatch.setenv("AGENT_PUBLIC_WS_URL", "wss://voice-agent.fly.dev/stream")
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    from src.handlers import twilio_voice_hook

    resp = twilio_voice_hook.handler(_twilio_event("From=%2B15555550100&CallSid=CA123"), None)
    assert resp["statusCode"] == 200
    assert "<Stream" in resp["body"] and "wss://voice-agent.fly.dev/stream" in resp["body"]
    assert 'name="caller"' in resp["body"] and "+15555550100" in resp["body"]


def test_voice_hook_rejects_bad_signature(monkeypatch):
    monkeypatch.setenv("AGENT_PUBLIC_WS_URL", "wss://x/stream")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "secret")
    from src.handlers import twilio_voice_hook

    resp = twilio_voice_hook.handler(_twilio_event("From=%2B1555", signature="forged"), None)
    assert resp["statusCode"] == 403


def test_voice_hook_accepts_valid_signature(monkeypatch):
    from twilio.request_validator import RequestValidator

    monkeypatch.setenv("AGENT_PUBLIC_WS_URL", "wss://x/stream")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "secret")
    from src.handlers import twilio_voice_hook

    url = "https://abc.execute-api.us-east-1.amazonaws.com/voice"
    sig = RequestValidator("secret").compute_signature(url, {"From": "+1555"})
    resp = twilio_voice_hook.handler(_twilio_event("From=%2B1555", signature=sig), None)
    assert resp["statusCode"] == 200


# ---------- Idempotency ----------


def test_duplicate_key_short_circuits():
    with patch("src.handlers._common.seen_before", return_value=True):
        from src.handlers import take_payment

        resp = take_payment.handler(_event({"amount_usd": 10, "description": "x", "customer_email": "a@b.co"}), None)
    assert json.loads(resp["body"])["status"] == "duplicate"


def test_validation_failure_releases_key(no_dynamo):
    from src.handlers import book_meeting

    resp = book_meeting.handler(_event({"caller_name": "Alice"}, idempotency_key="k1"), None)
    assert resp["statusCode"] == 400
    assert "missing fields" in json.loads(resp["body"])["error"]
    no_dynamo.assert_called_once_with("k1")


def test_exception_releases_key_and_reraises(no_dynamo, monkeypatch):
    from src.handlers import book_meeting

    monkeypatch.setattr(book_meeting, "_service", lambda: (_ for _ in ()).throw(RuntimeError("google down")))
    with pytest.raises(RuntimeError):
        book_meeting.handler(
            _event(
                {
                    "caller_name": "A",
                    "caller_email": "a@b.co",
                    "start_iso": "2026-10-06T14:00:00",
                    "topic": "Demo",
                },
                idempotency_key="k2",
            ),
            None,
        )
    no_dynamo.assert_called_once_with("k2")


# ---------- Payment guardrails ----------


def test_payment_rejects_over_limit(no_dynamo, monkeypatch):
    monkeypatch.setattr("src.handlers.take_payment.MAX_AMOUNT_USD", 100.0)
    from src.handlers import take_payment

    resp = take_payment.handler(_event({"amount_usd": 500, "description": "x", "customer_email": "a@b.co"}), None)
    assert resp["statusCode"] == 400 and "limit" in json.loads(resp["body"])["error"]


def test_log_lead_requires_phone(no_dynamo):
    from src.handlers import log_lead

    resp = log_lead.handler(_event({"first_name": "Alice"}), None)
    assert resp["statusCode"] == 400
    assert "missing field: phone" in json.loads(resp["body"])["error"]
