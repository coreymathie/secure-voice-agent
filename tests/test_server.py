# Corey Mathie, 2026
"""Local dev server: the /voice webhook returns stream TwiML and enforces Twilio signatures."""

import pytest

pytest.importorskip("pipecat")

from fastapi.testclient import TestClient

from src.agent import server


@pytest.fixture
def client():
    return TestClient(server.app)


def test_voice_returns_stream_with_caller(client, monkeypatch):
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    r = client.post("/voice", data={"From": "+15555550100", "CallSid": "CA1"})
    assert r.status_code == 200
    assert "<Stream" in r.text and 'name="caller"' in r.text and "+15555550100" in r.text


def test_voice_rejects_forged_request(client, monkeypatch):
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "secret")
    r = client.post("/voice", data={"From": "+1555"}, headers={"X-Twilio-Signature": "forged"})
    assert r.status_code == 403


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}
