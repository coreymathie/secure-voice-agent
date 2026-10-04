# Corey Mathie, 2026
"""
What a caller hears before the agent says anything: a fixed AI disclosure and,
when the call may be recorded, a recording-consent step. Both come from TwiML
played by Twilio, not from the model, so they happen on every call regardless of
what the model does.

    <Say> disclosure (+ "this call may be recorded" when no consent prompt is needed) </Say>
    recording consent needed?
      yes: <Gather numDigits=1 action=/consent> "press 1 to allow recording, 2 to continue without" </Gather>
           → /consent → <Connect><Stream> with consent=granted | declined | no_input
      no:  <Connect><Stream> with consent=not_required (one-party, notice given) or not_requested (no recording)

The voice process records the result in the audit log (`call_disclosure`) and
starts a recording only if recording_allowed() says so (src/agent/disclosure.py).
Silence or any key other than 1 means no recording.

Jurisdiction comes from Twilio's FromState parameter, which is derived from the
caller's *number*, not where the caller is. An unknown state is treated as
all-party. The default all-party list is the commonly cited one and includes
states where the rule is disputed or differs by call type; it is a configurable
starting point, not legal advice. RECORDING_CONSENT_MODE=always (the default)
asks every caller and doesn't depend on the list at all.

Standard library only: the Lambda handlers, the local server, and the browser
demo all use this file.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from xml.sax.saxutils import escape, quoteattr

# Commonly cited all-party ("two-party") consent states. Conservative: includes states whose rule is disputed
# or applies only to some call types. Verify for your deployment; override with RECORDING_ALL_PARTY_STATES.
ALL_PARTY_STATES: tuple[str, ...] = (
    "CA",
    "CT",
    "DE",
    "FL",
    "IL",
    "MD",
    "MA",
    "MI",
    "MT",
    "NV",
    "NH",
    "OR",
    "PA",
    "WA",
)
CONSENT_MODES: tuple[str, ...] = ("always", "by_jurisdiction", "off")
DEFAULT_DISCLOSURE = "Hi, you've reached an automated assistant. You're talking with an A.I., not a person."
RECORDING_NOTICE = "This call may be recorded."
CONSENT_PROMPT = (
    "We'd like to record this call to check the quality of our service. Press 1 to allow recording, "
    "or press 2 to continue without recording."
)
CONSENT_ACK = {
    "granted": "Thank you.",
    "declined": "Okay, this call won't be recorded.",
    "no_input": "Okay, this call won't be recorded.",
}
CONSENT_RESULTS: tuple[str, ...] = ("granted", "declined", "no_input", "not_required", "not_requested")


@dataclass(frozen=True)
class CallStartConfig:
    disclosure: str = DEFAULT_DISCLOSURE
    recording_enabled: bool = False  # this repo never records unless a deployment turns it on
    consent_mode: str = "always"
    all_party: frozenset[str] = field(default_factory=lambda: frozenset(ALL_PARTY_STATES))
    business_jurisdiction: str | None = None  # if the business is in an all-party state, ask everyone
    consent_timeout_seconds: int = 6

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> CallStartConfig:
        env = os.environ if env is None else env
        d = cls()
        mode = env.get("RECORDING_CONSENT_MODE", d.consent_mode).strip().lower()
        states = env.get("RECORDING_ALL_PARTY_STATES", "").strip()
        business = env.get("BUSINESS_JURISDICTION", "").strip().upper()
        return cls(
            disclosure=env.get("AI_DISCLOSURE_TEXT", "").strip() or d.disclosure,
            recording_enabled=env.get("RECORDING_ENABLED", "false").strip().lower() == "true",
            consent_mode=mode if mode in CONSENT_MODES else d.consent_mode,
            all_party=frozenset(s.strip().upper() for s in states.split(",") if s.strip()) if states else d.all_party,
            business_jurisdiction=business or None,
        )


def jurisdiction_of(form: Mapping[str, str]) -> str:
    """Two-letter state from Twilio's FromState (number-based), or 'unknown'."""
    state = str(form.get("FromState") or "").strip().upper()
    return state if len(state) == 2 and state.isalpha() else "unknown"


def needs_consent(jurisdiction: str, cfg: CallStartConfig) -> bool:
    """Ask before recording? Unknown jurisdictions count as all-party."""
    if not cfg.recording_enabled or cfg.consent_mode == "off":
        return False
    if cfg.consent_mode == "always":
        return True
    if cfg.business_jurisdiction and cfg.business_jurisdiction in cfg.all_party:
        return True
    return jurisdiction == "unknown" or jurisdiction in cfg.all_party


def initial_consent(jurisdiction: str, cfg: CallStartConfig) -> str:
    """Consent state when no prompt is played: not_requested (no recording) or not_required (one-party notice)."""
    if not cfg.recording_enabled or cfg.consent_mode == "off":
        return "not_requested"
    return "pending" if needs_consent(jurisdiction, cfg) else "not_required"


def consent_from_digits(digits: str | None) -> str:
    d = str(digits or "").strip()
    return "granted" if d == "1" else "declined" if d else "no_input"


def recording_allowed(consent: str, jurisdiction: str, cfg: CallStartConfig) -> bool:
    """The single rule for starting a recording. Re-derived from config, not trusted from stream parameters."""
    if not cfg.recording_enabled or cfg.consent_mode == "off":
        return False
    if consent == "granted":
        return True
    return consent == "not_required" and not needs_consent(jurisdiction, cfg)


def _a(pairs: list[tuple[str, str]]) -> str:
    return "".join(f" {k}={quoteattr(str(v))}" for k, v in pairs)


def stream_twiml(stream_url: str, caller: str, consent: str, jurisdiction: str, say: str = "") -> str:
    params = [("caller", caller), ("disclosure", "given"), ("consent", consent), ("jurisdiction", jurisdiction)]
    inner = "".join(f"<Parameter{_a([('name', k), ('value', v)])} />" for k, v in params)
    lead = f"<Say>{escape(say)}</Say>" if say else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response>{lead}<Connect><Stream{_a([('url', stream_url)])}>{inner}</Stream></Connect></Response>"
    )


def build_voice_twiml(
    stream_url: str, consent_url: str, form: Mapping[str, str], cfg: CallStartConfig | None = None
) -> str:
    """The incoming-call TwiML: disclosure first, then either the consent prompt or the stream."""
    cfg = cfg or CallStartConfig()
    caller = str(form.get("From") or "unknown")
    jurisdiction = jurisdiction_of(form)
    consent = initial_consent(jurisdiction, cfg)
    if consent != "pending":
        say = cfg.disclosure + (f" {RECORDING_NOTICE}" if consent == "not_required" else "")
        return stream_twiml(stream_url, caller, consent, jurisdiction, say=say)
    gather = _a(
        [
            ("input", "dtmf"),
            ("numDigits", "1"),
            ("timeout", str(cfg.consent_timeout_seconds)),
            ("action", consent_url),
            ("actionOnEmptyResult", "true"),
        ]
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response><Say>{escape(cfg.disclosure)}</Say>"
        f"<Gather{gather}><Say>{escape(CONSENT_PROMPT)}</Say></Gather>"
        f"<Redirect>{escape(consent_url)}</Redirect></Response>"
    )


def build_consent_twiml(stream_url: str, form: Mapping[str, str]) -> tuple[str, str]:
    """TwiML for the consent action URL, and the consent result it recorded."""
    consent = consent_from_digits(form.get("Digits"))
    caller = str(form.get("From") or "unknown")
    return stream_twiml(stream_url, caller, consent, jurisdiction_of(form), say=CONSENT_ACK[consent]), consent
