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
        assert s["steps"] or "call_start" in s, s["id"]
        for step in s["steps"]:
            known = step["tool"] in harness.HANDLERS or step["tool"] in harness.STEP_UP_TOOLS
            assert known and "expect" in step, (s["id"], step)


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


# ---------- Policy gate (0.5.0) ----------

POLICY_SCENARIOS = {
    "social-engineering-handoff",
    "per-call-payment-cap",
    "unlisted-tool-default-deny",
    "action-cap-per-call",
}


def test_evals_catch_a_missing_policy_gate(monkeypatch):
    from src.safeguards import policy_gate

    def allow_everything(self, tool, args=None):
        return policy_gate.Decision(policy_gate.ALLOW, "ok", tool, "gate removed", "low")

    monkeypatch.setattr(policy_gate.CallPolicy, "decide", allow_everything)
    assert POLICY_SCENARIOS <= _failed()


def test_evals_catch_missing_risk_scoring(monkeypatch):
    from src.safeguards import policy_gate

    monkeypatch.setattr(policy_gate, "score_turns", lambda turns, *a, **k: policy_gate.RiskAssessment(0, {}))
    assert "social-engineering-handoff" in _failed()


def test_evals_catch_per_call_counters_not_updating(monkeypatch):
    from src.safeguards import policy_gate

    monkeypatch.setattr(policy_gate.CallPolicy, "record_outcome", lambda self, *a, **k: None)
    assert {"per-call-payment-cap", "action-cap-per-call"} <= _failed()


def _policy_with(tmp_path, old: str, new: str):
    """A copy of config/policy.yaml with one edit, the way a reviewer would propose a change."""
    from src.safeguards.policy_config import load_policy

    text = harness.POLICY_PATH.read_text()
    assert old in text, old
    path = tmp_path / "proposed.yaml"
    path.write_text(text.replace(old, new, 1))
    return load_policy(path)


def test_evals_catch_an_over_eager_risk_threshold(tmp_path):
    assert "urgent-but-benign" in _failed(policy=_policy_with(tmp_path, "  threshold: 4", "  threshold: 1"))


# ---------- Step-up verification and account takeover (0.6.0) ----------


def test_evals_catch_step_up_being_skipped(monkeypatch):
    from src.safeguards import policy_gate

    monkeypatch.setattr(policy_gate.PolicyConfig, "needs_step_up", lambda self, tier: False)
    assert {"caller-id-is-not-identity", "step-up-then-pay"} <= _failed()


def test_evals_catch_codes_sent_to_caller_id(monkeypatch):
    from src.safeguards import step_up

    real = step_up.StepUpSession.record

    def caller_id_as_identity(self):
        record = real(self)
        return record and step_up.CustomerRecord(record.customer_id, phone_on_file=self.caller_id)

    monkeypatch.setattr(step_up.StepUpSession, "record", caller_id_as_identity)
    assert "caller-id-is-not-identity" in _failed()


def test_evals_catch_a_missing_lockout(tmp_path):
    proposed = _policy_with(tmp_path, "max_failed_attempts: 3", "max_failed_attempts: 99")
    assert "otp-lockout" in _failed(policy=proposed)


def test_evals_catch_a_missing_takeover_rule(monkeypatch):
    from src.safeguards import policy_gate

    monkeypatch.setattr(policy_gate, "CONTACT_CHANGED", frozenset())
    assert "account-takeover-sequence" in _failed()


def test_evals_catch_an_ignored_sim_swap_signal(monkeypatch):
    from src.safeguards import step_up

    monkeypatch.setattr(step_up.StaticRiskSignals, "signals", lambda self, record, destination: frozenset())
    assert "sim-swap-signal" in _failed()


# ---------- PCI keypad capture (0.6.0) ----------


def test_evals_catch_card_capture_without_pausing_the_recording(monkeypatch):
    from src.handlers import keypad_payment

    monkeypatch.setattr(keypad_payment, "pause_recording", lambda client, call_sid: False)
    assert {"keypad-payment", "keypad-recording-pause-fails"} <= _failed()


# ---------- AI disclosure and recording consent (0.6.0) ----------

CONSENT_SCENARIOS = {"consent-declined-no-recording", "consent-no-input-no-recording"}


def test_evals_catch_recording_without_consent(monkeypatch):
    from src.agent import disclosure

    monkeypatch.setattr(disclosure, "recording_allowed", lambda consent, jurisdiction, cfg: True)
    assert CONSENT_SCENARIOS <= _failed()


def test_evals_catch_silence_treated_as_consent(monkeypatch):
    from src.handlers import call_start

    monkeypatch.setattr(call_start, "consent_from_digits", lambda digits: "declined" if digits == "2" else "granted")
    assert "consent-no-input-no-recording" in _failed()


def test_evals_catch_a_missing_ai_disclosure(monkeypatch):
    from dataclasses import replace

    from src.handlers import call_start

    real = call_start.CallStartConfig.from_env
    monkeypatch.setattr(
        call_start.CallStartConfig, "from_env", classmethod(lambda cls, env=None: replace(real(env), disclosure=""))
    )
    assert {"ai-disclosure-always", "consent-declined-no-recording"} <= _failed()


# ---------- Shared velocity store (0.6.0) ----------


def test_every_scenario_passes_with_the_redis_velocity_store(monkeypatch):
    from src.safeguards.velocity_redis import RedisVelocityStore
    from tests.fake_redis import FakeRedis

    monkeypatch.setattr(
        harness,
        "VelocityStore",
        lambda rules, clock: RedisVelocityStore(FakeRedis(), rules=rules, clock=clock, salt="eval"),
    )
    failures = {r.id: r.failures for r in harness.run_all() if not r.passed}
    assert not failures, failures
