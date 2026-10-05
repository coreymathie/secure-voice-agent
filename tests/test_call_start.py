# Corey Mathie, 2026
"""AI disclosure and recording consent: TwiML, the consent rules, the webhooks, and the voice-process audit."""

import base64
import json
import xml.etree.ElementTree as ET
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from src.agent import disclosure
from src.handlers import call_start as cs
from src.safeguards.audit_log import AuditLog

STREAM = "wss://agent.example.com/stream"
CONSENT = "https://api.example.com/consent"
REC = cs.CallStartConfig(recording_enabled=True, consent_mode="by_jurisdiction")


def _root(xml: str) -> ET.Element:
    return ET.fromstring(xml)


def _params(xml: str) -> dict:
    return {p.attrib["name"]: p.attrib["value"] for p in _root(xml).iter("Parameter")}


# ---------- Rules ----------


@pytest.mark.parametrize(
    "jurisdiction, cfg, expected",
    [
        ("CA", REC, True),
        ("TX", REC, False),
        ("unknown", REC, True),  # unknown counts as all-party
        ("TX", cs.CallStartConfig(recording_enabled=True, consent_mode="always"), True),
        ("CA", cs.CallStartConfig(recording_enabled=False), False),
        ("CA", cs.CallStartConfig(recording_enabled=True, consent_mode="off"), False),
        (
            "TX",
            cs.CallStartConfig(recording_enabled=True, consent_mode="by_jurisdiction", business_jurisdiction="FL"),
            True,
        ),
    ],
)
def test_needs_consent(jurisdiction, cfg, expected):
    assert cs.needs_consent(jurisdiction, cfg) is expected


@pytest.mark.parametrize(
    "consent, jurisdiction, cfg, allowed",
    [
        ("granted", "CA", REC, True),
        ("declined", "CA", REC, False),
        ("no_input", "CA", REC, False),
        ("not_required", "TX", REC, True),
        ("not_required", "CA", REC, False),  # a forged "not_required" for an all-party state is refused
        ("not_required", "TX", cs.CallStartConfig(recording_enabled=True, consent_mode="always"), False),
        ("granted", "CA", cs.CallStartConfig(recording_enabled=False), False),
        ("granted", "CA", cs.CallStartConfig(recording_enabled=True, consent_mode="off"), False),
        ("missing", "TX", REC, False),
    ],
)
def test_recording_allowed(consent, jurisdiction, cfg, allowed):
    assert cs.recording_allowed(consent, jurisdiction, cfg) is allowed


@pytest.mark.parametrize(
    "digits, result", [("1", "granted"), ("2", "declined"), ("9", "declined"), ("", "no_input"), (None, "no_input")]
)
def test_consent_from_digits(digits, result):
    assert cs.consent_from_digits(digits) == result


def test_jurisdiction_comes_from_from_state_only():
    assert cs.jurisdiction_of({"FromState": "ca"}) == "CA"
    assert cs.jurisdiction_of({"FromState": "California"}) == "unknown"
    assert cs.jurisdiction_of({}) == "unknown"


def test_config_from_env():
    cfg = cs.CallStartConfig.from_env(
        {
            "RECORDING_ENABLED": "true",
            "RECORDING_CONSENT_MODE": "by_jurisdiction",
            "RECORDING_ALL_PARTY_STATES": "ca, wa",
            "BUSINESS_JURISDICTION": "tx",
            "AI_DISCLOSURE_TEXT": "You're speaking with an AI assistant for Example Plumbing.",
        }
    )
    assert cfg.recording_enabled and cfg.consent_mode == "by_jurisdiction"
    assert cfg.all_party == frozenset({"CA", "WA"}) and cfg.business_jurisdiction == "TX"
    assert "Example Plumbing" in cfg.disclosure
    d = cs.CallStartConfig.from_env({"RECORDING_CONSENT_MODE": "sometimes", "AI_DISCLOSURE_TEXT": "  "})
    assert d.consent_mode == "always" and d.disclosure == cs.DEFAULT_DISCLOSURE and not d.recording_enabled


# ---------- TwiML ----------


def test_default_call_plays_the_disclosure_then_connects_without_recording_notice():
    xml = cs.build_voice_twiml(STREAM, CONSENT, {"From": "+15555550100", "FromState": "TX"})
    root = _root(xml)
    assert [c.tag for c in root] == ["Say", "Connect"]
    assert root.find("Say").text == cs.DEFAULT_DISCLOSURE and "may be recorded" not in xml
    assert _params(xml) == {
        "caller": "+15555550100",
        "disclosure": "given",
        "consent": "not_requested",
        "jurisdiction": "TX",
    }


def test_all_party_state_gets_the_consent_prompt_after_the_disclosure():
    xml = cs.build_voice_twiml(STREAM, CONSENT, {"From": "+15555550100", "FromState": "CA"}, REC)
    root = _root(xml)
    assert [c.tag for c in root] == ["Say", "Gather", "Redirect"]
    g = root.find("Gather")
    assert g.attrib["numDigits"] == "1" and g.attrib["action"] == CONSENT and g.attrib["actionOnEmptyResult"] == "true"
    assert "Press 1" in g.find("Say").text and root.find("Redirect").text == CONSENT
    assert root.find("Connect") is None  # the agent isn't connected until consent is answered


def test_one_party_state_gets_a_notice():
    xml = cs.build_voice_twiml(STREAM, CONSENT, {"From": "+1", "FromState": "TX"}, REC)
    assert cs.RECORDING_NOTICE in _root(xml).find("Say").text and _params(xml)["consent"] == "not_required"


def test_consent_twiml_carries_the_result():
    xml, consent = cs.build_consent_twiml(STREAM, {"From": "+15555550100", "FromState": "CA", "Digits": "2"})
    assert consent == "declined" and _params(xml)["consent"] == "declined"
    assert "won't be recorded" in _root(xml).find("Say").text


def test_twiml_escapes_configured_text():
    cfg = cs.CallStartConfig(disclosure='AI assistant for "Tom & Jo\'s" <Plumbing>')
    xml = cs.build_voice_twiml(STREAM, CONSENT, {"From": "+1"}, cfg)
    assert _root(xml).find("Say").text == cfg.disclosure


def test_twiml_attribute_names_match_the_twilio_helper_library():
    from twilio.twiml.voice_response import Gather, VoiceResponse

    ours = _root(cs.build_voice_twiml(STREAM, CONSENT, {"From": "+1", "FromState": "CA"}, REC)).find("Gather")
    ref = VoiceResponse()
    ref.append(Gather(input="dtmf", num_digits=1, timeout=6, action=CONSENT, action_on_empty_result=True))
    assert set(ours.attrib) == set(_root(str(ref)).find("Gather").attrib)


# ---------- Webhooks ----------


def _event(form: dict, path: str, signature: str = ""):
    return {
        "body": base64.b64encode(urlencode(form).encode()).decode(),
        "isBase64Encoded": True,
        "headers": {"X-Twilio-Signature": signature},
        "rawPath": path,
        "requestContext": {"domainName": "abc.execute-api.us-east-1.amazonaws.com"},
    }


@pytest.fixture
def hook_env(monkeypatch):
    monkeypatch.setenv("AGENT_PUBLIC_WS_URL", STREAM)
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("RECORDING_ENABLED", "true")
    monkeypatch.setenv("RECORDING_CONSENT_MODE", "always")


def test_voice_hook_points_the_consent_prompt_at_the_sibling_route(hook_env):
    from src.handlers import twilio_voice_hook

    resp = twilio_voice_hook.handler(_event({"From": "+15555550100", "FromState": "NY"}, "/prod/voice"), None)
    g = _root(resp["body"]).find("Gather")
    assert g.attrib["action"] == "https://abc.execute-api.us-east-1.amazonaws.com/prod/consent"


def test_consent_lambda_verifies_twilio_and_connects(hook_env, monkeypatch):
    from twilio.request_validator import RequestValidator

    from src.handlers import twilio_consent

    form = {"From": "+15555550100", "FromState": "CA", "Digits": "1"}
    ok = twilio_consent.handler(_event(form, "/consent"), None)
    assert _params(ok["body"])["consent"] == "granted"
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "secret")
    assert twilio_consent.handler(_event(form, "/consent", "forged"), None)["statusCode"] == 403
    url = "https://abc.execute-api.us-east-1.amazonaws.com/consent"
    sig = RequestValidator("secret").compute_signature(url, form)
    assert twilio_consent.handler(_event(form, "/consent", sig), None)["statusCode"] == 200


def test_local_server_has_the_consent_route(hook_env):
    pytest.importorskip("pipecat")
    from fastapi.testclient import TestClient

    from src.agent import server

    client = TestClient(server.app)
    r = client.post("/voice", data={"From": "+15555550100", "FromState": "CA"})
    assert "<Gather" in r.text and "/consent" in r.text
    r = client.post("/consent", data={"From": "+15555550100", "FromState": "CA"})
    assert 'name="consent" value="no_input"' in r.text


# ---------- Voice process ----------


class FakeTwilio:
    def __init__(self, fail=False):
        self.started, self.fail = [], fail

    def calls(self, sid):
        def create(**kw):
            if self.fail:
                raise RuntimeError("recording API down")
            self.started.append(sid)

        return SimpleNamespace(recordings=SimpleNamespace(create=create))


@pytest.fixture
def audit(tmp_path):
    return AuditLog(tmp_path / "audit.jsonl")


def _entry(audit):
    rows = [json.loads(line) for line in audit.path.read_text().splitlines()]
    return [r for r in rows if r["event"] == "call_disclosure"][-1]["payload"]


@pytest.mark.parametrize(
    "params, started, recording",
    [
        ({"disclosure": "given", "consent": "granted", "jurisdiction": "CA"}, 1, "started"),
        ({"disclosure": "given", "consent": "declined", "jurisdiction": "CA"}, 0, "refused_no_consent"),
        ({"disclosure": "given", "consent": "not_required", "jurisdiction": "TX"}, 1, "started"),
        ({"disclosure": "given", "consent": "not_required", "jurisdiction": "CA"}, 0, "refused_no_consent"),
        ({"consent": "granted", "jurisdiction": "CA"}, 0, "refused_no_consent"),  # no disclosure, no recording
        ({"disclosure": "given", "consent": "made-up", "jurisdiction": "CA"}, 0, "refused_no_consent"),
    ],
)
def test_on_call_start_records_only_with_consent(audit, params, started, recording):
    fake = FakeTwilio()
    out = disclosure.on_call_start(params, "CA1", "ref", audit, cfg=REC, client_factory=lambda: fake)
    assert len(fake.started) == started and out["recording"] == recording == _entry(audit)["recording"]


def test_on_call_start_audits_disclosure_missing_and_disabled_recording(audit):
    out = disclosure.on_call_start({}, "CA1", "ref", audit, cfg=cs.CallStartConfig())
    assert out == {"disclosure": "missing", "consent": "missing", "jurisdiction": "unknown", "recording": "disabled"}


def test_recording_failure_is_audited_not_raised(audit):
    params = {"disclosure": "given", "consent": "granted", "jurisdiction": "CA"}
    out = disclosure.on_call_start(params, "CA1", "ref", audit, cfg=REC, client_factory=lambda: FakeTwilio(fail=True))
    assert out["recording"] == "failed" and "recording API down" in _entry(audit)["error"]


async def test_run_bot_audits_call_start_but_not_on_resume(monkeypatch, tmp_path):
    pytest.importorskip("pipecat")
    from unittest.mock import MagicMock

    from src.agent import bot, tools

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("AGENT_PROVIDER", "openai_realtime")
    monkeypatch.setenv("AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setattr(tools, "_audit", None)
    seen = []
    monkeypatch.setattr(bot, "on_call_start", lambda params, *a: seen.append(params))

    async def nothing(*a, **k):
        return None

    monkeypatch.setattr(bot.WorkerRunner, "run", nothing)
    monkeypatch.setattr(bot, "finalize_call", nothing)
    ws = MagicMock()
    ws.headers = {}
    params = {"caller": "+1", "disclosure": "given", "consent": "declined"}
    await bot.run_bot(ws, "MZ1", "CA1", "+1", stream_params=params)
    await bot.run_bot(ws, "MZ2", "CA1", "+1", resume="keypad_payment", pay_result="success", stream_params=params)
    assert seen == [params]


def test_system_prompt_never_claims_to_be_a_person():
    from src.agent.provider import DEFAULT_SYSTEM_PROMPT

    assert "never claim to be a person" in DEFAULT_SYSTEM_PROMPT
