# Corey Mathie, 2026
"""Tests for the safeguards module."""

import json
import tempfile
from pathlib import Path

from src.safeguards.audit_log import AuditLog
from src.safeguards.pii_redactor import redact
from src.safeguards.velocity import VelocityRule, VelocityStore

# ---------- PII redactor ----------


def test_redact_ssn():
    out, c = redact("my ssn is 123-45-6789")
    assert "[REDACTED_SSN]" in out
    assert c["ssn"] == 1


def test_redact_pan_luhn_only():
    # Valid test PAN (passes Luhn)
    out, c = redact("card 4532015112830366")
    assert "[REDACTED_PAN]" in out
    assert c.get("pan") == 1

    # Random long number that fails Luhn — should not be redacted
    out, c = redact("order number 1234567890123456")
    assert "1234567890123456" in out
    assert "pan" not in c


def test_redact_email_and_phone():
    out, c = redact("reach me at a@b.co or 305-555-0142")
    assert "[REDACTED_EMAIL]" in out and "[REDACTED_PHONE]" in out
    assert c["email"] == 1 and c["phone"] == 1


def test_allowlist_bypasses_rule():
    out, c = redact("email a@b.co", allow=("email",))
    assert "a@b.co" in out
    assert "email" not in c


# ---------- Audit log ----------


def test_audit_chain_valid():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "audit.jsonl"
        log = AuditLog(path)
        log.append("call_start", {"caller": "c1"})
        log.append("tool_call", {"tool": "book_meeting"})
        log.append("call_end", {"caller": "c1"})
        ok, bad = log.verify_chain()
        assert ok is True and bad is None


def test_audit_chain_detects_tampering():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "audit.jsonl"
        log = AuditLog(path)
        log.append("x", {"a": 1})
        log.append("y", {"b": 2})
        log.append("z", {"c": 3})

        # Tamper with line 2
        lines = path.read_text().splitlines()
        row = json.loads(lines[1])
        row["payload"] = {"b": 999}
        lines[1] = json.dumps(row, separators=(",", ":"))
        path.write_text("\n".join(lines) + "\n")

        ok, bad = log.verify_chain()
        assert ok is False and bad == 2


# ---------- Velocity ----------


def test_velocity_denies_rapid_payment():
    rules = (VelocityRule(tool="take_payment", max_count=1, window_seconds=60),)
    v = VelocityStore(rules=rules)
    allowed, _ = v.check("caller-A", "take_payment")
    assert allowed is True
    allowed, reason = v.check("caller-A", "take_payment")
    assert allowed is False
    assert "velocity" in reason


def test_velocity_per_caller_isolation():
    rules = (VelocityRule(tool="take_payment", max_count=1, window_seconds=60),)
    v = VelocityStore(rules=rules)
    v.check("caller-A", "take_payment")
    allowed, _ = v.check("caller-B", "take_payment")
    assert allowed is True
