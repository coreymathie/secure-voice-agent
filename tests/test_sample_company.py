# Corey Mathie, 2026
"""The console's sample-company data: reproducible, internally consistent, and labelled fictional."""

import json
import re
from pathlib import Path

from scripts import generate_sample_company as gen

ROOT = Path(__file__).resolve().parents[1]


def test_committed_file_matches_the_generator():
    assert gen.main(["--check"]) == 0


def test_generation_is_deterministic():
    assert gen.render(gen.build()) == gen.render(gen.build())


def test_daily_outcomes_add_up():
    data = gen.build()
    assert len(data["days"]) == gen.DAYS
    for d in data["days"]:
        assert d["contained"] + d["transferred"] + d["abandoned"] == d["calls"]
        assert 0 <= d["after_hours"] <= d["calls"]
        assert d["stepup_passed"] <= d["stepups"]
        assert d["card_numbers_scrubbed"] <= d["pii_scrubbed"]
        assert 0 <= d["recording_declined"] <= d["calls"]
        # Fraud attempts are the three fraud controls; verification lockouts are counted on their own.
        assert d["fraud_blocked"] == d["sim_swap_holds"] + d["social_engineering_handoffs"] + d["takeover_patterns"]


def test_it_is_labelled_fictional_and_assumptions_are_stated():
    data = json.loads((ROOT / "demo" / "data" / "sample_company.json").read_text())
    assert data["company"]["fictional"] is True
    assert "not measurements" in data["disclaimer"]
    assert data["assumptions"]["agent_cost_per_call_usd"] > data["assumptions"]["ai_cost_per_call_usd"]


def test_recent_calls_are_reproducible_and_on_the_last_day():
    data = gen.build_calls()
    assert gen.render_calls(data) == gen.render_calls(gen.build_calls())
    calls = data["calls"]
    assert len(calls) == gen.RECENT_CALLS
    starts = [c["started"] for c in calls]
    assert starts == sorted(starts, reverse=True)
    through = gen.build()["recent_calls"]["through"]
    assert starts[0] == through
    assert all(s.startswith(gen.END.isoformat()) and s <= through for s in starts)
    assert len({c["id"] for c in calls}) == len(calls)


def test_recent_calls_use_only_fictional_contact_details():
    text = gen.render_calls(gen.build_calls())
    assert not re.search(r"\b\d{13,16}\b", text.replace(" ", "")), "no full card numbers"
    for c in gen.build_calls()["calls"]:
        assert re.fullmatch(r"\(\d{3}\) 555-01\d\d", c["member"]["phone"])
    assert set(re.findall(r"@([\w.-]+)", text)) <= {"example.com"}


def test_recent_calls_match_the_dashboard():
    company = gen.build()
    calls = gen.build_calls()["calls"]
    intents = {i["intent"] for i in company["intents"]}
    assert {c["intent"] for c in calls} <= intents
    resolved = sum(c["outcome"] == "resolved" for c in calls) / len(calls)
    last = company["days"][-1]
    assert abs(resolved - last["contained"] / last["calls"]) < 0.08
    for c in calls:
        assert (c["outcome"] == "transferred") == bool(c["transfer_reason"])
        assert c["transcript"][0]["who"] == "system", "the AI disclosure comes first"
        assert c["safeguards"][-1]["control"] == "Audit log"
        if "Fraud stopped" in c["flags"]:
            assert c["outcome"] == "transferred"
            assert not any(a["tool"] == "take_payment" for a in c["actions"])
