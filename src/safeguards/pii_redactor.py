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


# ---------- Numbers read out as words ----------
#
# Speech-to-text often writes a card number the way it was said: "four one one one, one one one one, ...",
# "five one oh five", "double four", "cuatro uno uno uno". Runs of digit words (English and Spanish; "oh" for
# zero; "double" / "triple" / "doble" repeat the next digit; digit groups may be mixed in) are read as one
# number. A run that is a Luhn-valid 13-19 digit card number is redacted as `pan`, and a run of exactly 9
# digits as `ssn`, the same as the digit rules above. Other runs (a phone number, an amount said digit by
# digit) are left as they are. Not covered: numbers said in pairs or tens ("forty-one eleven"), a card
# number run straight into the expiry date with no word between, and languages other than English and Spanish.

_DIGIT_WORDS = {
    "zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
    "seven": "7", "eight": "8", "nine": "9",
    "cero": "0", "uno": "1", "una": "1", "dos": "2", "tres": "3", "cuatro": "4", "cinco": "5", "seis": "6",
    "siete": "7", "ocho": "8", "nueve": "9",
}  # fmt: skip
_REPEAT = {"double": 2, "triple": 3, "doble": 2}
_UNIT = r"(?:(?:double|triple|doble)\s+)?(?:" + "|".join(_DIGIT_WORDS) + r"|\d{1,4})"
_RUN = re.compile(rf"\b{_UNIT}(?:[\s,.\-]+{_UNIT})+\b", re.IGNORECASE)
_TOKEN = re.compile(rf"\b({_UNIT})\b", re.IGNORECASE)


def _token_digits(token: str) -> str:
    parts = token.lower().split()
    times = _REPEAT.get(parts[0], 1) if len(parts) == 2 else 1
    word = parts[-1]
    return (word if word.isdigit() else _DIGIT_WORDS[word]) * times


def _spoken_numbers(text: str, allow: tuple[str, ...], counts: dict[str, int]) -> str:
    out: list[str] = []
    last = 0
    for run in _RUN.finditer(text):
        tokens = list(_TOKEN.finditer(run.group(0)))
        if all(t.group(1).isdigit() for t in tokens):
            continue  # digits only: the digit rules handle it
        number = "".join(_token_digits(t.group(1)) for t in tokens)
        if "pan" not in allow and 13 <= len(number) <= 19 and _luhn_valid(number):
            rule = "pan"
        elif "ssn" not in allow and len(number) == 9:
            rule = "ssn"
        else:
            continue
        start, end = run.start() + tokens[0].start(), run.start() + tokens[-1].end()
        out.append(text[last:start])
        out.append("[REDACTED_PAN]" if rule == "pan" else "[REDACTED_SSN]")
        counts[rule] = counts.get(rule, 0) + 1
        last = end
    out.append(text[last:])
    return "".join(out)


def redact(text: str, allow: tuple[str, ...] = ()) -> tuple[str, dict[str, int]]:
    """
    Redact PII patterns in `text`.

    Returns the redacted text + a counter of how many times each rule fired.
    `allow` lets a caller opt-out of specific rules by name — the agent may
    legitimately need to log an email to a tool call, for example.
    """
    counts: dict[str, int] = {}
    out = _spoken_numbers(text, allow, counts)
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
