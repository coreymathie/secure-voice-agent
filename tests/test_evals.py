# Corey Mathie, 2026
"""
The call evals run in CI through pytest too. The mutation tests switch off one
safeguard at a time and check that the evals notice, so a passing scorecard
means something.
"""

import pytest

from evals import harness
from src.agent import tools
from src.safeguards.velocity import VelocityStore


def _failed(**kw) -> set[str]:
    return {r.id for r in harness.run_all(**kw) if not r.passed}


def test_every_scenario_passes():
    results = harness.run_all()
    assert len(results) >= 10
    failures = {r.id: r.failures for r in results if not r.passed}
    assert not failures, failures


def test_scenario_file_is_well_formed():
    for s in harness.load_scenarios():
        assert s["steps"], s["id"]
        for step in s["steps"]:
            assert step["tool"] in harness.HANDLERS and "expect" in step, (s["id"], step)


def test_evals_catch_pii_reaching_outside_services(monkeypatch):
    monkeypatch.setattr(tools, "scrub_args", lambda args: (args, {}))
    assert "card-number-in-ticket" in _failed()


def test_evals_catch_missing_velocity_limits(monkeypatch):
    monkeypatch.setattr(VelocityStore, "check", lambda self, *a, **k: (True, None))
    assert {"payment-burst", "payment-ladder", "booking-burst"} <= _failed()


def test_evals_catch_retries_being_punished(monkeypatch):
    monkeypatch.setattr(VelocityStore, "release", lambda self, *a, **k: None)
    # Payments allow one request per two minutes, so a retry right after a no-op attempt needs the release.
    assert {"stripe-outage-retry", "over-limit-then-corrected"} <= _failed()


def test_evals_catch_lost_idempotency(monkeypatch):
    monkeypatch.setattr(harness.MemoryTable, "put_item", lambda self, **kw: None)
    assert "replayed-request" in _failed()


@pytest.mark.parametrize("bad_status", ["link_sent", "error"])
def test_a_wrong_expectation_fails(bad_status):
    scenario = {
        "id": "wrong",
        "title": "wrong",
        "steps": [{"tool": "book_meeting", "args": {}, "expect": bad_status}],
    }
    (result,) = harness.run_all([scenario])
    assert not result.passed and "expected" in result.failures[0]
