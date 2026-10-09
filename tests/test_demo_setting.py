# Corey Mathie, 2026
"""
The test line, the guided scenarios, the simulated callers and the call evals all play out at the sample
credit union: Florida members, South Florida phone numbers, the agent named Harbor, credit-union products,
and dates that follow the calendar instead of a fixed day that has already passed.
"""

import re
from datetime import date, datetime
from pathlib import Path

import pytest

from demo import engine as demo
from evals import harness, scripted, simulate
from scripts import sample_calls

ROOT = Path(__file__).resolve().parents[1]
SOUTH_FLORIDA = r"\+1(954|754|305|786|561)55501\d\d"
OTHER_BUSINESSES = re.compile(
    r"\b(service call|full install|install deposit|repair|quote|demo|filter|equipment|service plan|"
    r"annual service|deposit|leak|kitchen|garage|attic|porch|plumbing|roof\w*|estimate|overdraft fee)\b",
    re.IGNORECASE,
)


def _start_isos(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "start_iso" and isinstance(v, str) and v[:4].isdigit():
                yield v
            else:
                yield from _start_isos(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _start_isos(v)


# ---------- dates follow the calendar ----------


@pytest.mark.parametrize(
    ("today", "said", "expected"),
    [
        (date(2026, 10, 5), "Tuesday at 2pm", "2026-10-06T14:00:00"),  # a Monday: tomorrow
        (date(2026, 10, 6), "Tuesday at 2pm", "2026-10-13T14:00:00"),  # a Tuesday: next week, never today
        (date(2027, 3, 4), "tomorrow at 10", "2027-03-05T10:00:00"),
    ],
)
def test_test_line_dates_are_relative_to_today(monkeypatch, today, said, expected):
    monkeypatch.setattr(scripted, "CLOCK", lambda: today)
    assert demo._when(said) == expected


def test_test_line_never_books_in_the_past():
    booked = datetime.fromisoformat(demo._when("Tuesday at 2pm"))
    assert booked.date() > date.today()


def test_scenario_and_persona_dates_resolve_against_an_injected_clock(monkeypatch):
    monkeypatch.setattr(scripted, "CLOCK", lambda: date(2031, 1, 1))  # a Wednesday
    personas = simulate.load_personas()
    booker = next(p for p in personas if p["id"] == "benign-booker")
    assert booker["turns"][1]["wants"]["args"]["start_iso"] == "2031-01-07T14:00:00"  # "Tuesday at 2pm"
    burst = next(s for s in harness.load_scenarios() if s["id"] == "booking-burst")
    assert burst["steps"][0]["args"]["start_iso"] == "2031-01-02T09:00:00-04:00"
    consent = next(s for s in demo.resolved_scenarios() if s["id"] == "consent")
    assert list(_start_isos(consent)) == ["2031-01-03T09:00:00"]  # the next Friday


def test_no_fixed_dates_in_scenarios_personas_or_templates():
    for path in ("evals/scenarios.yaml", "evals/personas.yaml", "demo/app.js"):
        text = (ROOT / path).read_text()
        assert not re.search(r"20\d\d-\d\d-\d\dT", text), path
    assert not any(v[:4].isdigit() for v in _start_isos(demo.SCENARIOS))
    today = date.today()
    resolved = (
        list(_start_isos(demo.resolved_scenarios()))
        + list(_start_isos(harness.load_scenarios()))
        + list(_start_isos(simulate.load_personas()))
    )
    assert resolved and all(datetime.fromisoformat(v).date() > today for v in resolved)


# ---------- the sample credit union's setting ----------


def test_test_line_greets_as_harbor_and_uses_south_florida_numbers():
    assert f"this is {sample_calls.AGENT}" in demo.GREETING and demo.AGENT_NAME == sample_calls.AGENT
    assert re.fullmatch(SOUTH_FLORIDA, demo.DEMO_CALLER) and re.fullmatch(SOUTH_FLORIDA, demo.DEMO_PHONE_ON_FILE)
    for branch in ("Las Olas and", "Las Olas)"):
        assert branch not in demo.HOURS_REPLY
    assert "Fort Lauderdale main office" in demo.HOURS_REPLY


def test_guided_scenarios_take_place_in_florida():
    starts = [st["call_start"] for s in demo.SCENARIOS for st in s["steps"] if "call_start" in st]
    assert starts and all(st["state"] == "FL" for st in starts)
    for s in demo.SCENARIOS:
        assert not OTHER_BUSINESSES.search(str(s)), s["id"]


def test_eval_scenarios_and_personas_use_credit_union_terms_and_numbers():
    for path in (harness.SCENARIOS_PATH, simulate.PERSONAS_PATH):
        text = path.read_text()
        assert not OTHER_BUSINESSES.search(text), (path.name, OTHER_BUSINESSES.search(text))
        assert "customers:" not in text and "Customer" not in text
        for number in re.findall(r"\+1\d{10}", text):
            assert re.fullmatch(SOUTH_FLORIDA, number), (path.name, number)
    titles = {s["id"]: s["title"] for s in harness.load_scenarios()}
    assert titles["step-up-then-pay"] == "Member verifies with a code, then pays"


def test_tests_use_credit_union_terms():
    for name in ("test_call_start.py", "test_summary.py", "test_console_server.py", "test_tools.py", "test_keypad.py"):
        text = (ROOT / "tests" / name).read_text()
        assert not OTHER_BUSINESSES.search(text), (name, OTHER_BUSINESSES.search(text))
    smoke = (ROOT / "scripts" / "demo_smoke.py").read_text()
    assert not re.search(r"annual service|\$50 deposit|555-555-01", smoke)


def test_simulated_callers_have_titles_for_the_business_view():
    console = demo.Console(personas_text=simulate.PERSONAS_PATH.read_text())
    listed = console.persona_list()
    assert all(p["title"] and p["title"] != p["id"] for p in listed)
    detail = console.run_persona("benign-payer")
    assert detail["title"] == detail["persona"]["title"] == "Member pays this month's personal loan"


def test_member_record_is_the_step_up_record():
    from src.safeguards import step_up

    assert step_up.MemberRecord is step_up.CustomerRecord  # the earlier name still imports
    assert "CustomerRecord(" not in (ROOT / "demo" / "engine.py").read_text()
