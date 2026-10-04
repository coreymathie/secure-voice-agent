# Corey Mathie, 2026
"""
Wiring tests against the real Pipecat package: the pipeline builds, every tool is
registered with a safeguarded handler, and each provider's services construct.
Skipped automatically if Pipecat isn't installed.
"""

from unittest.mock import MagicMock

import pytest

pytest.importorskip("pipecat")


@pytest.fixture
def env(monkeypatch):
    for k, v in {
        "OPENAI_API_KEY": "sk-test",
        "ANTHROPIC_API_KEY": "sk-ant-test",
        "DEEPGRAM_API_KEY": "dg-test",
        "ELEVENLABS_API_KEY": "el-test",
        "ELEVENLABS_VOICE_ID": "voice123",
        "GOOGLE_API_KEY": "g-test",
    }.items():
        monkeypatch.setenv(k, v)


@pytest.mark.parametrize("provider", ["openai_realtime", "anthropic_11labs", "gemini_live"])
def test_each_provider_builds_services(env, provider):
    from src.agent.provider import get_provider

    services = get_provider(provider).build_services()
    assert "llm" in services
    if provider == "anthropic_11labs":
        assert {"stt", "tts"} <= set(services)


@pytest.mark.parametrize("with_twilio_creds", [True, False])
def test_pipeline_registers_safeguarded_tools(env, monkeypatch, with_twilio_creds):
    from src.agent import bot, tools

    monkeypatch.setenv("AGENT_PROVIDER", "openai_realtime")
    if with_twilio_creds:
        monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC123")
        monkeypatch.setenv("TWILIO_AUTH_TOKEN", "token")
    else:
        monkeypatch.delenv("TWILIO_ACCOUNT_SID", raising=False)
        monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    registered = {}
    real_make = tools.make_handler

    def spy_make(name, caller_id, executors=None, **kw):
        h = real_make(name, caller_id, executors, **kw)
        registered[name] = (caller_id, kw.get("outcomes"))
        return h

    monkeypatch.setattr(bot, "make_handler", spy_make)
    websocket = MagicMock()
    websocket.headers = {}
    call = bot.build_pipeline(websocket, stream_sid="MZ1", call_sid="CA1", caller_id="+15555550100")

    assert call.task is not None and call.transport is not None
    assert set(registered) == set(tools.TOOL_NAMES)
    assert all(cid == "+15555550100" for cid, _ in registered.values())
    # Every handler reports into the same per-call outcome list the summary reads.
    assert all(outcomes is call.outcomes for _, outcomes in registered.values())
    assert call.context.get_messages()[0]["content"] == bot.KICKOFF_MESSAGE


async def test_call_is_summarized_even_if_the_pipeline_crashes(env, monkeypatch):
    from src.agent import bot

    monkeypatch.setenv("AGENT_PROVIDER", "openai_realtime")
    captured = {}

    async def crash(self, *args, **kwargs):
        raise RuntimeError("websocket dropped")

    async def fake_finalize(messages, outcomes, call_sid, caller_id):
        captured.update(messages=messages, outcomes=outcomes, call_sid=call_sid, caller_id=caller_id)

    monkeypatch.setattr(bot.WorkerRunner, "run", crash)
    monkeypatch.setattr(bot, "finalize_call", fake_finalize)
    websocket = MagicMock()
    websocket.headers = {}
    with pytest.raises(RuntimeError):
        await bot.run_bot(websocket, "MZ1", "CA7", "+19545550101")
    assert captured["call_sid"] == "CA7" and captured["messages"][0]["content"] == bot.KICKOFF_MESSAGE
