# Corey Mathie, 2026
"""Tool Lambdas accept only requests signed by the voice process."""

import json

import pytest

from src.agent import tools
from src.handlers import _common, log_lead

SECRET = "test-signing-secret"
NOW = 1_800_000_000


@pytest.fixture
def signed_env(monkeypatch):
    monkeypatch.setenv("TOOL_API_SECRET", SECRET)
    monkeypatch.delenv("ALLOW_UNSIGNED_TOOL_CALLS", raising=False)


def _event(body: bytes, headers: dict) -> dict:
    return {"body": body.decode(), "headers": headers}


def _signed(payload: dict, key: str = "idem-1", now: float = NOW) -> tuple[bytes, dict]:
    body = json.dumps(payload, separators=(",", ":")).encode()
    return body, _common.signed_headers(SECRET, key, body, now=now)


def test_valid_signature_is_accepted(signed_env):
    body, headers = _signed({"a": 1})
    assert _common.verify_signature(_event(body, headers), now=NOW + 10) is None


def test_tool_request_signs_when_secret_is_set(signed_env):
    body, headers = tools.tool_request({"first_name": "Rae"}, "idem-9")
    assert headers[_common.SIGNATURE_HEADER].startswith("v1=")
    assert _common.verify_signature(_event(body, headers)) is None


def test_missing_secret_rejects_everything(monkeypatch):
    monkeypatch.delenv("TOOL_API_SECRET", raising=False)
    monkeypatch.delenv("ALLOW_UNSIGNED_TOOL_CALLS", raising=False)
    body, headers = _signed({"a": 1})
    assert _common.verify_signature(_event(body, headers), now=NOW) == "TOOL_API_SECRET is not configured"


def test_unsigned_request_is_rejected(signed_env):
    assert _common.verify_signature(_event(b"{}", {"Idempotency-Key": "k"}), now=NOW) == "missing signature"


def test_tampered_body_is_rejected(signed_env):
    body, headers = _signed({"customer_phone": "+15555550100"})
    forged = body.replace(b"0100", b"0199")
    assert _common.verify_signature(_event(forged, headers), now=NOW) == "bad signature"


def test_replay_under_new_idempotency_key_is_rejected(signed_env):
    body, headers = _signed({"a": 1}, key="idem-1")
    headers["Idempotency-Key"] = "idem-2"
    assert _common.verify_signature(_event(body, headers), now=NOW) == "bad signature"


def test_stale_timestamp_is_rejected(signed_env):
    body, headers = _signed({"a": 1}, now=NOW)
    later = NOW + _common.MAX_SKEW_SECONDS + 1
    assert _common.verify_signature(_event(body, headers), now=later) == "stale timestamp"


def test_wrong_secret_is_rejected(signed_env):
    body = b'{"a":1}'
    headers = _common.signed_headers("not-the-secret", "k", body, now=NOW)
    assert _common.verify_signature(_event(body, headers), now=NOW) == "bad signature"


def test_handler_returns_401_and_does_not_claim_the_key(signed_env, monkeypatch):
    claimed = []
    monkeypatch.setattr(_common, "seen_before", lambda key: claimed.append(key) or False)
    resp = log_lead.handler({"body": "{}", "headers": {"Idempotency-Key": "k"}}, None)
    assert resp["statusCode"] == 401
    assert json.loads(resp["body"]) == {"error": "unauthorized"}
    assert claimed == []
