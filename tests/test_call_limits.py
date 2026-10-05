# Corey Mathie, 2026
"""Maximum call duration and idle timeout: the timer, the watcher's goodbye sequence, and the bot wiring."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from src.agent.call_limits import CONTROL_PREFIX, INSTRUCTIONS, CallLimits, CallTimer, watch_call


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def timer(**limits):
    clock = Clock()
    return CallTimer(CallLimits(**limits), clock=clock), clock


# ---------- Timer ----------


def test_idle_prompts_then_ends():
    t, clock = timer(idle_prompt_seconds=15, idle_timeout_seconds=30, max_duration_seconds=0)
    clock.now = 14
    assert t.poll() == []
    clock.now = 15
    assert t.poll() == ["idle_prompt"]
    clock.now = 20
    assert t.poll() == []  # prompts once
    clock.now = 30
    assert t.poll() == ["end:idle"] and t.ended == "idle"
    clock.now = 99
    assert t.poll() == []  # ends once


def test_caller_speech_resets_idle_and_the_prompt():
    t, clock = timer(idle_prompt_seconds=15, idle_timeout_seconds=30, max_duration_seconds=0)
    clock.now = 16
    assert t.poll() == ["idle_prompt"]
    t.caller_activity()
    clock.now = 30
    assert t.poll() == []  # 14 s since the caller spoke
    clock.now = 31
    assert t.poll() == ["idle_prompt"]


def test_agent_speech_pauses_idle_and_silence_counts_from_its_end():
    t, clock = timer(idle_prompt_seconds=0, idle_timeout_seconds=30, max_duration_seconds=0)
    clock.now = 5
    t.set_bot_speaking(True)
    clock.now = 60
    assert t.poll() == []  # the agent has been talking, the caller isn't idle
    t.set_bot_speaking(False)
    clock.now = 89
    assert t.poll() == []
    clock.now = 90
    assert t.poll() == ["end:idle"]


def test_max_duration_warns_then_ends_even_while_people_talk():
    t, clock = timer(max_duration_seconds=900, duration_warning_seconds=60, idle_timeout_seconds=30)
    for second in range(0, 900, 10):
        clock.now = second
        t.caller_activity()
        actions = t.poll()
        assert actions == (["duration_warning"] if second == 840 else []), second
    clock.now = 900
    assert t.poll() == ["end:max_duration"]


def test_zero_turns_limits_off():
    t, clock = timer(max_duration_seconds=0, idle_timeout_seconds=0, idle_prompt_seconds=0)
    clock.now = 10**6
    assert t.poll() == []


def test_started_at_carries_over_a_resumed_call():
    clock = Clock()
    clock.now = 1000
    t = CallTimer(CallLimits(max_duration_seconds=900), clock=clock, started_at=150)
    assert t.elapsed() == 850 and t.poll() == ["duration_warning"]


def test_limits_from_env():
    lim = CallLimits.from_env(
        {"CALL_MAX_DURATION_SECONDS": "600", "CALL_IDLE_TIMEOUT_SECONDS": "0", "CALL_IDLE_PROMPT_SECONDS": "-3"}
    )
    assert lim.max_duration_seconds == 600 and lim.idle_timeout_seconds == 0 and lim.idle_prompt_seconds == 15
    assert CallLimits.from_env({"CALL_MAX_DURATION_SECONDS": "nan"}).max_duration_seconds == 900
    assert CallLimits.from_env({"CALL_LIMIT_POLL_SECONDS": "0"}).poll_seconds == 0.05


def test_instructions_are_marked_and_never_part_of_the_transcript():
    from src.agent.summary import transcript_from_messages

    assert all(text.startswith(CONTROL_PREFIX) for text in INSTRUCTIONS.values())
    msgs = [{"role": "user", "content": INSTRUCTIONS["idle"]}, {"role": "user", "content": "hello?"}]
    assert transcript_from_messages(msgs) == [("user", "hello?")]


# ---------- Watcher ----------


async def _run_watch(t, clock, *, speak_at=()):
    """Drive watch_call with a fake sleep that advances the fake clock."""
    said, ended, audited = [], [], []

    async def sleep(seconds):
        clock.now += seconds
        if int(clock.now) in speak_at:
            t.caller_activity()

    async def instruct(text):
        said.append((clock.now, text))

    async def end_call():
        ended.append(clock.now)

    reason = await asyncio.wait_for(
        watch_call(t, instruct, end_call, lambda r, e: audited.append((r, e)), sleep=sleep), timeout=5
    )
    return reason, said, ended, audited


async def test_idle_call_gets_prompt_goodbye_then_end():
    t, clock = timer(idle_prompt_seconds=15, idle_timeout_seconds=30, max_duration_seconds=0, goodbye_grace_seconds=8)
    reason, said, ended, audited = await _run_watch(t, clock)
    assert reason == "idle"
    assert [text for _, text in said] == [INSTRUCTIONS["idle_prompt"], INSTRUCTIONS["idle"]]
    assert audited == [("idle", 30.0)]
    assert ended == [38.0]  # goodbye first, then the grace period, then end


async def test_long_call_gets_warning_goodbye_then_end():
    t, clock = timer(max_duration_seconds=120, duration_warning_seconds=60, idle_timeout_seconds=30)
    reason, said, ended, audited = await _run_watch(t, clock, speak_at=range(0, 200, 5))
    assert reason == "max_duration"
    assert [text for _, text in said] == [INSTRUCTIONS["duration_warning"], INSTRUCTIONS["max_duration"]]
    assert audited == [("max_duration", 120.0)] and ended == [128.0]


# ---------- Bot wiring ----------


def test_activity_observer_feeds_the_timer():
    pytest.importorskip("pipecat")
    from pipecat.frames.frames import BotStartedSpeakingFrame, BotStoppedSpeakingFrame, UserStartedSpeakingFrame

    from src.agent.call_limits import make_activity_observer

    t, clock = timer()
    obs = make_activity_observer(t)
    clock.now = 10
    asyncio.run(obs.on_push_frame(SimpleNamespace(frame=UserStartedSpeakingFrame())))
    assert t.last_activity == 10
    asyncio.run(obs.on_push_frame(SimpleNamespace(frame=BotStartedSpeakingFrame())))
    assert t.bot_speaking
    clock.now = 20
    asyncio.run(obs.on_push_frame(SimpleNamespace(frame=BotStoppedSpeakingFrame())))
    assert not t.bot_speaking and t.last_activity == 20


@pytest.fixture
def bot_env(monkeypatch, tmp_path):
    pytest.importorskip("pipecat")
    from src.agent import bot, tools

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("AGENT_PROVIDER", "openai_realtime")
    monkeypatch.setenv("AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setattr(tools, "_audit", None)
    return bot, tmp_path / "audit.jsonl"


def test_pipeline_worker_gets_the_activity_observer(bot_env, monkeypatch):
    bot, _ = bot_env
    seen = {}
    real = bot.PipelineWorker

    def spy(*a, **kw):
        seen.update(kw)
        return real(*a, **kw)

    monkeypatch.setattr(bot, "PipelineWorker", spy)
    from unittest.mock import MagicMock

    ws = MagicMock()
    ws.headers = {}
    call = bot.build_pipeline(ws, "MZ1", "CA1", "+15555550100")
    # PipelineWorker appends its own observers to the list; ours must be among them.
    assert [type(o).__name__ for o in seen["observers"]].count("CallActivityObserver") == 1
    assert call.timer.ended is None


async def test_silent_call_is_ended_gracefully_and_audited(bot_env, monkeypatch):
    from unittest.mock import MagicMock

    from pipecat.frames.frames import EndFrame, LLMMessagesAppendFrame

    bot, audit_path = bot_env
    for k, v in {
        "CALL_IDLE_PROMPT_SECONDS": "0.1",
        "CALL_IDLE_TIMEOUT_SECONDS": "0.2",
        "CALL_GOODBYE_GRACE_SECONDS": "0.05",
        "CALL_LIMIT_POLL_SECONDS": "0.05",
    }.items():
        monkeypatch.setenv(k, v)
    queued = []
    real_build = bot.build_pipeline

    def build(*a, **kw):
        call = real_build(*a, **kw)

        async def queue_frames(frames):
            queued.extend(frames)

        async def queue_frame(frame):
            queued.append(frame)

        call.task.queue_frames = queue_frames
        call.task.queue_frame = queue_frame
        return call

    async def run_until_ended(self, *a, **k):
        for _ in range(100):
            if any(isinstance(f, EndFrame) for f in queued):
                return
            await asyncio.sleep(0.02)

    async def nothing(*a, **k):
        return None

    monkeypatch.setattr(bot, "build_pipeline", build)
    monkeypatch.setattr(bot.WorkerRunner, "run", run_until_ended)
    monkeypatch.setattr(bot, "finalize_call", nothing)
    ws = MagicMock()
    ws.headers = {}
    await bot.run_bot(ws, "MZ1", "CA1", "+15555550100")

    texts = [f.messages[0]["content"] for f in queued if isinstance(f, LLMMessagesAppendFrame)]
    assert texts == [INSTRUCTIONS["idle_prompt"], INSTRUCTIONS["idle"]]
    assert isinstance(queued[-1], EndFrame)
    rows = [json.loads(line) for line in audit_path.read_text().splitlines()]
    ended = [r["payload"] for r in rows if r["event"] == "call_ended_by_limit"]
    assert len(ended) == 1 and ended[0]["reason"] == "idle" and "+15555550100" not in audit_path.read_text()
