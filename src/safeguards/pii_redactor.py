# Corey Mathie, 2026
"""
PII redactor for transcripts and tool-call arguments.

Scrubs patterns commonly leaked over a voice channel BEFORE anything is
logged, sent to a third-party LLM provider, or persisted. Built from the
patterns that actually show up in fraud/dispute case notes: SSNs, PAN digits,
DOBs, routing + account numbers.

This is pattern-level defense. For regulated workloads also:
  - Set GATEWAY_LOG_PROMPTS=false in production
  - Terminate TLS at the load balancer
  - Scope the audit log to a separate volume with retention policy

Not a substitute for a formal DLP, but it closes the common foot-guns.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class RedactionRule:
    name: str
    pattern: re.Pattern
    replacement: str


_RULES: tuple[RedactionRule, ...] = (
    # SSN — 9 digits with dashes or spaces, or run-on
    RedactionRule(
        "ssn",
        re.compile(r"\b(\d{3})[- ]?(\d{2})[- ]?(\d{4})\b"),
        "[REDACTED_SSN]",
    ),
    # Credit card PAN — 13-19 digits with optional separators (Luhn-validated below).
    # Ends on a digit so the separator after the number is kept ("4111 ... 1111 was" stays readable).
    RedactionRule(
        "pan",
        re.compile(r"\b(?:\d[ -]?){12,18}\d\b"),
        "[REDACTED_PAN]",
    ),
    # US bank routing (9 digits) + account (8-17 digits) when they appear together
    RedactionRule(
        "bank_routing_account",
        re.compile(
            r"\b(routing|aba)[^\d]{0,10}(\d{9})[^\d]{0,20}(account)[^\d]{0,10}(\d{8,17})\b",
            re.IGNORECASE,
        ),
        "[REDACTED_BANK_ACCOUNT]",
    ),
    # DOB — mm/dd/yyyy or mm-dd-yyyy, with explicit DOB marker nearby
    RedactionRule(
        "dob",
        re.compile(
            r"\b(dob|birth[- ]?date|date of birth)[^\d]{0,10}(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})\b",
            re.IGNORECASE,
        ),
        "[REDACTED_DOB]",
    ),
    # Email — RFC 5322-ish, intentionally loose
    RedactionRule(
        "email",
        re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),
        "[REDACTED_EMAIL]",
    ),
    # US phone — various formats
    RedactionRule(
        "phone",
        re.compile(r"\b(?:\+?1[\s.-]?)?\(?[2-9]\d{2}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b"),
        "[REDACTED_PHONE]",
    ),
    # Driver's license — very loose, triggered by nearby marker only
    RedactionRule(
        "dl",
        re.compile(
            r"\b(driver['s]*|dl|license)[^A-Z0-9]{0,10}([A-Z0-9]{6,14})\b",
            re.IGNORECASE,
        ),
        "[REDACTED_DL]",
    ),
)


def _luhn_valid(digits: str) -> bool:
    """Standard Luhn check — avoids redacting random long numeric strings."""
    s = [int(c) for c in digits if c.isdigit()]
    if len(s) < 13 or len(s) > 19:
        return False
    checksum = 0
    for i, d in enumerate(reversed(s)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        checksum += d
    return checksum % 10 == 0


def redact(text: str, allow: tuple[str, ...] = ()) -> tuple[str, dict[str, int]]:
    """
    Redact PII patterns in `text`.

    Returns the redacted text + a counter of how many times each rule fired.
    `allow` lets a caller opt-out of specific rules by name — the agent may
    legitimately need to log an email to a tool call, for example.
    """
    counts: dict[str, int] = {}
    out = text
    for rule in _RULES:
        if rule.name in allow:
            continue

        def _sub(match: re.Match, rule: RedactionRule = rule) -> str:
            if rule.name == "pan" and not _luhn_valid(match.group(0)):
                return match.group(0)
            counts[rule.name] = counts.get(rule.name, 0) + 1
            return rule.replacement

        out = rule.pattern.sub(_sub, out)
    return out, counts


# Government and financial identifiers never leave the agent, even inside free text
# such as a ticket body. Emails and phone numbers are kept: the tools need them.
TOOL_ARG_ALLOW: tuple[str, ...] = ("email", "phone")


def scrub_args(args: dict) -> tuple[dict, dict[str, int]]:
    """Redact card numbers, SSNs, bank details, DOBs, and license numbers in string arguments."""
    clean: dict = {}
    counts: dict[str, int] = {}
    for k, v in args.items():
        if isinstance(v, str):
            v, c = redact(v, allow=TOOL_ARG_ALLOW)
            for rule, n in c.items():
                counts[rule] = counts.get(rule, 0) + n
        clean[k] = v
    return clean, counts
