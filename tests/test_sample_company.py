# Corey Mathie, 2026
"""The console's sample-company data: reproducible, internally consistent, and labelled fictional."""

import json
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
        assert d["fraud_blocked"] == (
            d["sim_swap_holds"] + d["social_engineering_handoffs"] + d["takeover_patterns"] + d["otp_lockouts"]
        )


def test_it_is_labelled_fictional_and_assumptions_are_stated():
    data = json.loads((ROOT / "demo" / "data" / "sample_company.json").read_text())
    assert data["company"]["fictional"] is True
    assert "not measurements" in data["disclaimer"]
    assert data["assumptions"]["agent_cost_per_call_usd"] > data["assumptions"]["ai_cost_per_call_usd"]
