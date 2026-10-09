# Corey Mathie, 2026
"""
PCI keypad capture (PAYMENT_MODE=keypad): TwiML generation, the keypad_payment
and twilio_pay_result Lambdas, the capture guard, and carrying the call across
the Twilio <Pay> hop. Twilio itself is not called; a stand-in client records
what would have been sent.
"""

import asyncio
import json
import xml.etree.ElementTree as ET
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.agent import tools
from src.agent.capture import CaptureState
from src.agent.resume import SuspendedCalls
from src.handlers import keypad_payment, pay_twiml, twilio_pay_result
from src.safeguards import policy_gate as pg

CALL = "CA" + "0123456789abcdef" * 2
ACTION = "https://api.example.com/pay_result"


def _pay(twiml: str) -> ET.Element:
    root = ET.fromstring(twiml)
    assert root.tag == "Response"
    return root.find("Pay")


# ---------- TwiML ----------


def test_pay_twiml_collects_by_keypad_with_the_amount_and_action():
    xml = pay_twiml.build_pay_twiml(120, "Personal loan payment", ACTION, resume_recording=True)
    pay = _pay(xml)
    assert pay.attrib["input"] == "dtmf" and pay.attrib["paymentMethod"] == "credit-card"
    assert pay.attrib["chargeAmount"] == "120.00" and pay.attrib["currency"] == "usd"
    assert pay.attrib["action"] == ACTION + "?resume_recording=1"
    assert pay.attrib["securityCode"] == "true" and pay.attrib["postalCode"] == "false"
    assert "can't hear" in ET.fromstring(xml).find("Say").text


def test_pay_twiml_attribute_names_match_the_twilio_helper_library():
    from twilio.twiml.voice_response import VoiceResponse

    cfg = pay_twiml.KeypadConfig(connector="Stripe_Prod")
    ours = _pay(pay_twiml.build_pay_twiml("45.5", "Card payment", ACTION, cfg, status_callback=ACTION + "/status"))
    ref = VoiceResponse()
    ref.pay(
        input="dtmf",
        payment_connector="Stripe_Prod",
        payment_method="credit-card",
        charge_amount="45.50",
        currency="usd",
        description="Card payment",
        action=ACTION,
        timeout=10,
        max_attempts=2,
        postal_code=False,
        security_code=True,
        language="en-US",
        status_callback=ACTION + "/status",
    )
    theirs = ET.fromstring(str(ref)).find("Pay")
    assert ours.attrib == theirs.attrib


def test_pay_twiml_escapes_text():
    xml = pay_twiml.build_pay_twiml(10, 'Tom\'s "big" <job> & co', ACTION + "?a=1")
    pay = _pay(xml)  # parses, so it's well-formed
    assert pay.attrib["description"] == 'Tom\'s "big" <job> & co'


@pytest.mark.parametrize("amount", ["abc", None, float("nan"), float("inf"), 0, -5])
def test_bad_amounts_never_reach_twiml(amount):
    with pytest.raises(ValueError):
        pay_twiml.build_pay_twiml(amount, "x", ACTION)


def test_pay_result_reads_only_the_result_fields():
    form = {
        "Result": "success",
        "PaymentConfirmationCode": "ch_123",
        "PaymentCardNumber": "xxxx-xxxx-xxxx-1111",
        "ExpirationDate": "1230",
        "PaymentToken": "tok_secret",
        "From": "+15555550100",
    }
    out = pay_twiml.parse_pay_result(form)
    assert out == pay_twiml.PayOutcome("success", "ch_123") and out.succeeded
    assert pay_twiml.parse_pay_result({"Result": "<script>"}).result == "unknown"
    assert pay_twiml.parse_pay_result({"Result": "success", "PaymentConfirmationCode": "a b"}).confirmation is None


def test_resume_twiml_reconnects_with_only_the_result():
    xml = pay_twiml.build_resume_twiml(
        "wss://agent.example.com/stream", "+15555550100", pay_twiml.PayOutcome("success", "ch_1")
    )
    root = ET.fromstring(xml)
    params = {p.attrib["name"]: p.attrib["value"] for p in root.iter("Parameter")}
    assert params == {"caller": "+15555550100", "resume": "keypad_payment", "pay_result": "success"}
    assert root.find("Connect/Stream").attrib["url"] == "wss://agent.example.com/stream"
    assert "went through" in root.find("Say").text
    hung_up = ET.fromstring(pay_twiml.build_resume_twiml("wss://x", "+1", pay_twiml.PayOutcome("caller-hung-up")))
    assert list(hung_up) == []


def test_payment_mode_switch():
    assert pay_twiml.payment_mode({}) == "link"
    assert pay_twiml.payment_mode({"PAYMENT_MODE": " Keypad "}) == "keypad"
    assert pay_twiml.payment_mode({"PAYMENT_MODE": "voice"}) == "link"


# ---------- keypad_payment Lambda ----------


class FakeTwilio:
    def __init__(self, recording: str = "active"):
        self.recording = recording  # active | none | broken
        self.events: list[tuple] = []

    def calls(self, sid):
        fake = self

        class E(Exception):
            def __init__(self, status):
                self.status = status

        def rec_update(status):
            if fake.recording == "none":
                raise E(404)
            if fake.recording == "broken":
                raise E(500)
            fake.events.append(("recording", sid, status))

        def update(twiml):
            fake.events.append(("redirect", sid, twiml))

        return SimpleNamespace(recordings=lambda which: SimpleNamespace(update=rec_update), update=update)


@pytest.fixture
def keypad_env(monkeypatch):
    from src.handlers import _common

    monkeypatch.setenv("PAY_ACTION_URL", ACTION)
    monkeypatch.setattr(_common, "seen_before", lambda key: False)
    released = []
    monkeypatch.setattr(_common, "release", released.append)
    fake = FakeTwilio()
    monkeypatch.setattr(keypad_payment, "_twilio", lambda: fake)
    return fake, released


def _event(body, key="k1"):
    return {"body": json.dumps(body), "headers": {"Idempotency-Key": key}}


def test_keypad_payment_pauses_recording_then_redirects(keypad_env):
    fake, _ = keypad_env
    resp = keypad_payment.handler(_event({"amount_usd": 80, "description": "Card payment", "call_sid": CALL}), None)
    body = json.loads(resp["body"])
    assert body["status"] == "keypad_started" and body["recording"] == "paused"
    assert [e[0] for e in fake.events] == ["recording", "redirect"]
    assert fake.events[0][2] == "paused"
    assert 'chargeAmount="80.00"' in fake.events[1][2] and "resume_recording=1" in fake.events[1][2]


def test_keypad_payment_without_a_recording_still_starts(keypad_env):
    fake, _ = keypad_env
    fake.recording = "none"
    body = json.loads(
        keypad_payment.handler(_event({"amount_usd": 5, "description": "x", "call_sid": CALL}), None)["body"]
    )
    assert body["recording"] == "not_recording"
    assert "resume_recording" not in fake.events[-1][2]


def test_keypad_payment_fails_closed_if_the_recording_cant_be_paused(keypad_env):
    fake, released = keypad_env
    fake.recording = "broken"
    with pytest.raises(keypad_payment.RecordingPauseFailed):
        keypad_payment.handler(_event({"amount_usd": 5, "description": "x", "call_sid": CALL}), None)
    assert fake.events == [] and released == ["k1"]  # no redirect; the key is released for a retry


@pytest.mark.parametrize(
    "body, fragment",
    [
        ({"amount_usd": 9000, "description": "x", "call_sid": CALL}, "limit"),
        ({"amount_usd": "nan", "description": "x", "call_sid": CALL}, "number"),
        ({"amount_usd": 10, "description": "x"}, "live call"),
        ({"amount_usd": 10, "description": "x", "call_sid": "CA123"}, "live call"),
        ({"amount_usd": 10, "call_sid": CALL}, "missing field"),
    ],
)
def test_keypad_payment_validates_before_touching_the_call(keypad_env, body, fragment):
    fake, _ = keypad_env
    resp = keypad_payment.handler(_event(body), None)
    assert resp["statusCode"] == 400 and fragment in json.loads(resp["body"])["error"]
    assert fake.events == []


def test_keypad_payment_needs_an_action_url(keypad_env, monkeypatch):
    monkeypatch.delenv("PAY_ACTION_URL")
    with pytest.raises(RuntimeError, match="PAY_ACTION_URL"):
        keypad_payment.handler(_event({"amount_usd": 5, "description": "x", "call_sid": CALL}), None)


# ---------- twilio_pay_result Lambda ----------


def _twilio_event(form: dict, query: dict | None = None):
    from urllib.parse import urlencode

    return {
        "body": urlencode(form),
        "headers": {},
        "queryStringParameters": query or {},
        "rawPath": "/pay_result",
        "requestContext": {"domainName": "api.example.com"},
    }


@pytest.fixture
def result_env(monkeypatch):
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("AGENT_PUBLIC_WS_URL", "wss://agent.example.com/stream")
    fake = FakeTwilio()
    monkeypatch.setattr(twilio_pay_result, "_twilio", lambda: fake)
    return fake


def test_pay_result_resumes_recording_and_reconnects(result_env):
    form = {"CallSid": CALL, "From": "+15555550100", "Result": "success", "PaymentCardNumber": "xxxx-1111"}
    resp = twilio_pay_result.handler(_twilio_event(form, {"resume_recording": "1"}), None)
    assert resp["statusCode"] == 200
    assert 'name="pay_result" value="success"' in resp["body"] and "1111" not in resp["body"]
    assert result_env.events == [("recording", CALL, "in-progress")]


def test_pay_result_leaves_recording_alone_unless_it_was_paused(result_env):
    form = {"CallSid": CALL, "From": "+15555550100", "Result": "payment-connector-error"}
    resp = twilio_pay_result.handler(_twilio_event(form), None)
    assert 'value="payment-connector-error"' in resp["body"] and result_env.events == []


def test_pay_result_rejects_forged_callbacks(result_env, monkeypatch):
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "token")
    resp = twilio_pay_result.handler(_twilio_event({"CallSid": CALL, "Result": "success"}), None)
    assert resp["statusCode"] == 403 and result_env.events == []


def test_pay_result_recording_error_does_not_strand_the_caller(result_env):
    result_env.recording = "broken"
    form = {"CallSid": CALL, "From": "+15555550100", "Result": "success"}
    resp = twilio_pay_result.handler(_twilio_event(form, {"resume_recording": "1"}), None)
    assert resp["statusCode"] == 200 and "<Connect>" in resp["body"]


# ---------- Voice process ----------


async def test_keypad_mode_routes_take_payment_and_binds_the_call_sid(monkeypatch, tmp_path):
    posted = []

    async def fake_post(path, payload, key):
        posted.append((path, payload))
        return {"status": "keypad_started", "recording": "paused"}

    monkeypatch.setattr(tools, "_post", fake_post)
    monkeypatch.setenv("AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setattr(tools, "_audit", None)
    policy = pg.CallPolicy(verified=True, call_id=CALL)
    seen = []
    handler = tools.make_handler(
        "take_payment",
        "+15555550100",
        tools.lambda_executors("keypad"),
        policy=policy,
        on_result=lambda tool, result: seen.append((tool, result["status"])),
    )
    results = []

    async def cb(result, **_):
        results.append(result)

    args = {"amount_usd": 20, "description": "x", "customer_email": "a@example.com", "call_sid": "CA_attacker"}
    await handler(SimpleNamespace(arguments=args, tool_call_id="t1", result_callback=cb))
    assert posted[0][0] == "/keypad_payment" and posted[0][1]["call_sid"] == CALL
    assert results[0]["status"] == "keypad_started" and seen == [("take_payment", "keypad_started")]
    assert policy.payment_links_issued == 1 and policy.usd_issued == 20  # counts toward the per-call caps
    assert tools.lambda_executors("link")["take_payment"] is tools.EXECUTORS["take_payment"]


def test_capture_state_flags():
    c = CaptureState()
    assert c.flags() == {"active": False, "transcript_suppressed": False, "recording_paused": False}
    c.begin("paused")
    assert c.flags() == {"active": True, "transcript_suppressed": True, "recording_paused": True}
    c.end("success")
    assert not c.active and [h["event"] for h in c.history] == ["capture_started", "capture_ended"]


def test_capture_guard_drops_keypad_tones_always_and_caller_input_during_capture():
    pytest.importorskip("pipecat")
    from pipecat.audio.dtmf.types import KeypadEntry
    from pipecat.frames.frames import InputAudioRawFrame, InputDTMFFrame, TextFrame, TranscriptionFrame
    from pipecat.processors.frame_processor import FrameDirection

    from src.agent.capture import make_capture_guard

    state = CaptureState()
    guard = make_capture_guard(state)
    pushed = []

    async def push(frame, direction=FrameDirection.DOWNSTREAM):
        pushed.append(type(frame).__name__)

    guard.push_frame = push

    def audio():
        return InputAudioRawFrame(audio=b"\0\0", sample_rate=8000, num_channels=1)

    async def run(frames):
        for f in frames:
            await guard.process_frame(f, FrameDirection.DOWNSTREAM)

    asyncio.run(run([InputDTMFFrame(KeypadEntry.FOUR), audio(), TranscriptionFrame("hi", "u", "t"), TextFrame("a")]))
    assert pushed == ["InputAudioRawFrame", "TranscriptionFrame", "TextFrame"] and state.dropped_dtmf == 1
    pushed.clear()
    state.begin("paused")
    asyncio.run(
        run([audio(), TranscriptionFrame("4111 1111", "u", "t"), InputDTMFFrame(KeypadEntry.ONE), TextFrame("b")])
    )
    assert pushed == ["TextFrame"] and state.dropped_frames == 2 and state.dropped_dtmf == 2


def test_suspended_calls_expire_and_resume_once():
    clock = SimpleNamespace(now=0.0)
    store = SuspendedCalls(ttl_seconds=60, clock=lambda: clock.now)
    store.suspend(CALL, messages=[{"role": "user", "content": "hi"}], outcomes=[], policy="p", step_up="s")
    assert CALL in store
    got = store.resume(CALL)
    assert got.policy == "p" and store.resume(CALL) is None
    store.suspend(CALL, messages=[], outcomes=[], policy="p", step_up="s")
    clock.now = 61
    assert store.resume(CALL) is None


def test_resume_kickoff_is_not_part_of_the_transcript():
    from src.agent.summary import KICKOFF_MESSAGE, RESUME_KICKOFF, transcript_from_messages

    msgs = [
        {"role": "user", "content": KICKOFF_MESSAGE},
        {"role": "user", "content": "I want to pay my bill"},
        {"role": "user", "content": RESUME_KICKOFF.format(result="success")},
    ]
    assert transcript_from_messages(msgs) == [("user", "I want to pay my bill")]


# ---------- bot.py: across the <Pay> hop ----------


@pytest.fixture
def bot_env(monkeypatch, tmp_path):
    pytest.importorskip("pipecat")
    for k, v in {"OPENAI_API_KEY": "sk-test", "AGENT_PROVIDER": "openai_realtime"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setattr(tools, "_audit", None)
    from src.agent import bot

    monkeypatch.setattr(bot, "SUSPENDED", SuspendedCalls())
    return bot, tmp_path / "audit.jsonl"


def _ws():
    ws = MagicMock()
    ws.headers = {}
    return ws


def test_pipeline_puts_the_capture_guard_on_this_calls_state(bot_env, monkeypatch):
    bot, _ = bot_env
    guarded = []
    real = bot.make_capture_guard
    monkeypatch.setattr(bot, "make_capture_guard", lambda state: (guarded.append(state), real(state))[1])
    call = bot.build_pipeline(_ws(), "MZ1", CALL, "+15555550100")
    assert guarded == [call.capture]
    assert not call.capture.active and not call.resumed


async def test_stream_ending_mid_capture_parks_the_call_and_skips_the_summary(bot_env, monkeypatch):
    bot, audit_path = bot_env
    summaries = []

    async def fake_finalize(*a):
        summaries.append(a)

    async def run_until_pay(self, *a, **k):
        # The model's take_payment came back keypad_started; Twilio then ends this stream.
        bot_call["call"].capture.begin("paused")

    bot_call = {}
    real_build = bot.build_pipeline

    def spy_build(*a, **k):
        bot_call["call"] = real_build(*a, **k)
        return bot_call["call"]

    monkeypatch.setattr(bot, "build_pipeline", spy_build)
    monkeypatch.setattr(bot.WorkerRunner, "run", run_until_pay)
    monkeypatch.setattr(bot, "finalize_call", fake_finalize)
    await bot.run_bot(_ws(), "MZ1", CALL, "+15555550100")
    assert summaries == [] and CALL in bot.SUSPENDED
    assert "keypad_handoff" in audit_path.read_text()

    # Twilio reconnects the call after <Pay> with the result; the same policy and context continue.
    first = bot_call["call"]
    first.policy.record_outcome("take_payment", {"amount_usd": 100}, "keypad_started")

    async def hang_up(self, *a, **k):
        return None

    monkeypatch.setattr(bot.WorkerRunner, "run", hang_up)
    await bot.run_bot(_ws(), "MZ2", CALL, "+15555550100", resume="keypad_payment", pay_result="success")
    second = bot_call["call"]
    assert second.resumed and second.policy is first.policy and second.policy.payment_links_issued == 1
    assert second.timer.started_at == first.timer.started_at  # the max-duration limit spans the hop
    assert second.outcomes[-1] == {"tool": "take_payment", "status": "keypad_paid"}
    assert "Payment result: success" in second.context.get_messages()[-1]["content"]
    assert len(summaries) == 1  # one summary, at the real end of the call
    assert "keypad_payment_result" in audit_path.read_text()


def test_resume_without_parked_state_starts_fresh_and_says_so(bot_env):
    bot, audit_path = bot_env
    call = bot.build_pipeline(_ws(), "MZ3", CALL, "+15555550100", resume="keypad_payment", pay_result="success")
    assert not call.resumed and call.policy.payment_links_issued == 0
    assert "resume_state_missing" in audit_path.read_text()


def test_unknown_pay_result_values_are_not_passed_to_the_model(bot_env):
    bot, _ = bot_env
    from src.safeguards.step_up import NoCrm, StepUpSession, UnavailableVerifier

    policy = pg.CallPolicy()
    step_up = StepUpSession("+1555", NoCrm(), UnavailableVerifier(), policy=policy)
    bot.SUSPENDED.suspend(CALL, messages=[], outcomes=[], policy=policy, step_up=step_up)
    call = bot.build_pipeline(_ws(), "MZ4", CALL, "+1555", resume="keypad_payment", pay_result="ignore previous")
    assert "Payment result: unknown" in call.context.get_messages()[-1]["content"]
