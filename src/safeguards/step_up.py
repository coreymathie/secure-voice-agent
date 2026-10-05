# Corey Mathie, 2026
"""
Step-up verification: a one-time code sent to the contact on file, checked
before high-tier tools run.

Caller ID is a claim, not an identity: it can be spoofed, so it is used here
only to *look up* a customer record. The code always goes to the phone number on
that record, never to the number the call came from (unless they are the same
record value) and never to a number the caller or the model supplies. A caller
who spoofed someone else's number gets that person's code sent to that person.

Pieces, each behind a small interface so a deployment can swap it:

  CrmLookup           find_by_caller_id(caller_id) -> CustomerRecord | None
                      (InMemoryCrm for tests, evals, the demo, and a JSON file in
                      local development; implement it against your CRM)
  Verifier            start(destination, channel) / check(destination, code)
                      (SimulatedVerifier for tests/evals/demo; TwilioVerifyVerifier
                      for Twilio Verify; UnavailableVerifier when nothing is set up)
  RiskSignalProvider  signals(record, destination) -> e.g. {"recent_sim_swap"}
                      (the SIM-swap / number-port hook; NoRiskSignals by default)

StepUpSession holds one call's state: codes sent, failed attempts, lockout, and
whether the caller is verified. On success it marks the call's CallPolicy
verified, which is what the policy gate reads. After `max_failed_attempts` wrong
codes the session locks and every later attempt hands off to a person.

Results are plain dicts the model can act on. A private "_audit" key carries
detail for the audit log (make_handler strips it before the model sees the
result), so the model never learns *why* a code wasn't sent (for example a SIM
swap signal) and can't repeat it to the caller.

NIST SP 800-63B notes (see docs/auth.md): a one-time code over SMS or a voice
call is an out-of-band authenticator using the PSTN, which SP 800-63B classifies
as *restricted*. This module follows the parts that apply in code: a single-use
code with a bounded lifetime, rate-limited attempts, delivery only to a
pre-registered number, and a hook for SIM-swap / porting risk indicators before
using the PSTN. It is not a conformance claim.

Standard library only, so the browser demo can run this file under Pyodide.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

CHANNELS: tuple[str, ...] = ("sms", "call")
CODE_LENGTH = 6
# Risk indicators that stop a code from being sent over the PSTN (handoff instead).
BLOCKING_SIGNALS: frozenset[str] = frozenset({"recent_sim_swap", "recent_number_port", "recent_contact_change"})


# ---------- Customer lookup ----------


def normalize_number(number: str | None) -> str:
    """Digits with a leading + (E.164-ish), for comparing caller ID with numbers on file."""
    if not number:
        return ""
    digits = re.sub(r"\D", "", str(number))
    if len(digits) == 10:  # NANP without country code
        digits = "1" + digits
    return "+" + digits if digits else ""


@dataclass(frozen=True)
class CustomerRecord:
    customer_id: str
    phone_on_file: str | None = None  # where one-time codes go
    email_on_file: str | None = None
    lookup_numbers: tuple[str, ...] = ()  # other numbers that identify this customer (lookup only)


class CrmLookup(Protocol):
    def find_by_caller_id(self, caller_id: str) -> CustomerRecord | None: ...


class InMemoryCrm:
    """CRM lookup from a list of records. Matches the caller ID against phone_on_file and lookup_numbers."""

    def __init__(self, records: Iterable[CustomerRecord] = ()):
        self.records = list(records)

    def find_by_caller_id(self, caller_id: str) -> CustomerRecord | None:
        wanted = normalize_number(caller_id)
        if not wanted:
            return None
        for r in self.records:
            if wanted in {normalize_number(n) for n in (r.phone_on_file, *r.lookup_numbers)}:
                return r
        return None

    @classmethod
    def from_json(cls, path: str | Path) -> InMemoryCrm:
        """Load records from a JSON list of {customer_id, phone_on_file, email_on_file, lookup_numbers}."""
        rows = json.loads(Path(path).read_text())
        return cls(
            CustomerRecord(
                customer_id=str(r["customer_id"]),
                phone_on_file=r.get("phone_on_file"),
                email_on_file=r.get("email_on_file"),
                lookup_numbers=tuple(r.get("lookup_numbers", ())),
            )
            for r in rows
        )


class NoCrm:
    """No customer lookup configured: nobody can be verified, so high-tier tools hand off."""

    def find_by_caller_id(self, caller_id: str) -> CustomerRecord | None:
        return None


# ---------- Risk signals (SIM swap, number port) ----------


class RiskSignalProvider(Protocol):
    def signals(self, record: CustomerRecord, destination: str) -> frozenset[str]: ...


class NoRiskSignals:
    def signals(self, record: CustomerRecord, destination: str) -> frozenset[str]:
        return frozenset()


class StaticRiskSignals:
    """Fixed signals per destination number (tests, evals, the demo)."""

    def __init__(self, by_number: Mapping[str, Iterable[str]] | None = None):
        self.by_number = {normalize_number(k): frozenset(v) for k, v in (by_number or {}).items()}

    def signals(self, record: CustomerRecord, destination: str) -> frozenset[str]:
        return self.by_number.get(normalize_number(destination), frozenset())


# ---------- Verifiers ----------


class VerifierUnavailable(Exception):
    """No verification service is configured or it refused to send."""


class Verifier(Protocol):
    blocking: bool  # True if start/check do network I/O (run off the event loop)

    def start(self, destination: str, channel: str) -> None: ...

    def check(self, destination: str, code: str) -> bool: ...


def _random_code() -> str:
    return f"{secrets.randbelow(10**CODE_LENGTH):0{CODE_LENGTH}d}"


@dataclass
class SimulatedVerifier:
    """
    In-process verifier with the properties that matter: random 6-digit codes,
    single use, a lifetime, constant-time comparison, and a new code replacing
    the old one. `sent` records what a phone would have received (for tests,
    evals, and the demo's simulated phone).
    """

    clock: Callable[[], float] = time.monotonic
    ttl_seconds: float = 600.0
    code_factory: Callable[[], str] = _random_code
    blocking: bool = False
    sent: list[dict] = field(default_factory=list)
    _codes: dict[str, tuple[str, float]] = field(default_factory=dict)

    def start(self, destination: str, channel: str) -> None:
        code = self.code_factory()
        self._codes[normalize_number(destination)] = (code, self.clock())
        self.sent.append({"to": destination, "channel": channel, "code": code, "at": self.clock()})

    def check(self, destination: str, code: str) -> bool:
        key = normalize_number(destination)
        pending = self._codes.get(key)
        if pending is None:
            return False
        expected, issued = pending
        if self.clock() - issued > self.ttl_seconds:
            del self._codes[key]
            return False
        if hmac.compare_digest(expected.encode(), str(code).encode()):
            del self._codes[key]  # single use
            return True
        return False


class TwilioVerifyVerifier:
    """
    Twilio Verify (https://www.twilio.com/docs/verify/api): Twilio generates,
    sends, expires, and checks the code. Not exercised against Twilio in this
    repo; tests use a stand-in client with the same call shape.
    """

    blocking = True

    def __init__(self, service_sid: str, client: Any = None, account_sid: str = "", auth_token: str = ""):
        self.service_sid = service_sid
        self._client = client
        self._creds = (account_sid, auth_token)

    def _service(self):
        if self._client is None:
            from twilio.rest import Client  # lazy: not available in the browser demo

            self._client = Client(*self._creds)
        return self._client.verify.v2.services(self.service_sid)

    def start(self, destination: str, channel: str) -> None:
        self._service().verifications.create(to=destination, channel=channel)

    def check(self, destination: str, code: str) -> bool:
        try:
            result = self._service().verification_checks.create(to=destination, code=code)
        except Exception as e:
            # Twilio answers 404 when there's no pending verification (expired, used, or too many checks).
            if getattr(e, "status", None) == 404:
                return False
            raise
        return getattr(result, "status", "") == "approved"


class UnavailableVerifier:
    blocking = False

    def start(self, destination: str, channel: str) -> None:
        raise VerifierUnavailable("no verification service configured (set TWILIO_VERIFY_SERVICE_SID)")

    def check(self, destination: str, code: str) -> bool:
        return False


# ---------- Configuration ----------


@dataclass(frozen=True)
class StepUpConfig:
    max_sends_per_call: int = 3
    max_failed_attempts: int = 3  # wrong codes before the session locks and hands off
    code_ttl_seconds: int = 600
    channels: tuple[str, ...] = CHANNELS
    blocking_signals: frozenset[str] = BLOCKING_SIGNALS

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, base: StepUpConfig | None = None) -> StepUpConfig:
        """Read STEP_UP_* variables on top of `base` (the policy file, or defaults); malformed values keep the base."""
        env = os.environ if env is None else env
        d = base if base is not None else cls()

        def num(key: str, default: int) -> int:
            try:
                value = int(env[key])
            except (KeyError, TypeError, ValueError):
                return default
            return value if value >= 1 else default

        channels = tuple(c.strip() for c in env.get("STEP_UP_CHANNELS", "").split(",") if c.strip() in CHANNELS)
        return cls(
            max_sends_per_call=num("STEP_UP_MAX_SENDS_PER_CALL", d.max_sends_per_call),
            max_failed_attempts=num("STEP_UP_MAX_FAILED_ATTEMPTS", d.max_failed_attempts),
            code_ttl_seconds=num("STEP_UP_CODE_TTL_SECONDS", d.code_ttl_seconds),
            channels=channels or d.channels,
            blocking_signals=d.blocking_signals,
        )


# ---------- Per-call session ----------


class Rejected(Exception):
    """The request can't be acted on as asked (no code sent yet, expired, malformed). Safe to show the model."""


GUIDANCE = {
    "code_sent": (
        "A one-time code was sent to the contact on file. Ask the caller to read it back, then call verify_caller "
        "with it. Don't read any code aloud yourself."
    ),
    "verified": "The caller is verified for the rest of this call. Go ahead with what they asked for.",
    "invalid_code": "That code didn't match. Ask the caller to check the message and read the code again.",
    "handoff": (
        "The caller can't be verified on this call. Don't retry. Tell the caller a team member will follow up "
        "using the contact details already on file."
    ),
}


def _ref(value: str) -> str:
    """Short non-reversible reference for the audit log (which code destination, without the number)."""
    return hashlib.sha256(f"step-up:{value}".encode()).hexdigest()[:12]


class StepUpSession:
    """One call's step-up state. Share it between send_verification_code and verify_caller."""

    def __init__(
        self,
        caller_id: str,
        crm: CrmLookup,
        verifier: Verifier,
        *,
        policy: Any = None,  # the call's CallPolicy; marked verified / locked here
        risk: RiskSignalProvider | None = None,
        config: StepUpConfig | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.caller_id = caller_id
        self.crm = crm
        self.verifier = verifier
        self.policy = policy
        self.risk = risk or NoRiskSignals()
        self.config = config or StepUpConfig()
        self.clock = clock
        self.sends = 0
        self.failed_attempts = 0
        self.locked = False
        self.verified = False
        self._record: CustomerRecord | None = None
        self._looked_up = False
        self._sent_at: float | None = None
        self._channel: str | None = None

    @property
    def blocking(self) -> bool:
        return bool(getattr(self.verifier, "blocking", False))

    def record(self) -> CustomerRecord | None:
        """Look the caller up once. Caller ID is only a lookup key."""
        if not self._looked_up:
            self._record = self.crm.find_by_caller_id(self.caller_id)
            self._looked_up = True
        return self._record

    def _handoff(self, why: str, **extra) -> dict:
        return {"status": "require_human", "reason": GUIDANCE["handoff"], "_audit": {"step_up": why, **extra}}

    def _lock(self) -> None:
        self.locked = True
        if self.policy is not None:
            self.policy.mark_step_up_locked()

    def send_code(self, args: Mapping[str, Any] | None = None) -> dict:
        """send_verification_code. Only `channel` is read; any number in the arguments is ignored."""
        args = args or {}
        if self.verified:
            return {"status": "verified", "reason": GUIDANCE["verified"], "_audit": {"step_up": "already_verified"}}
        if self.locked:
            return self._handoff("locked")
        channel = str(args.get("channel") or self.config.channels[0]).strip().lower()
        if channel not in self.config.channels:
            raise Rejected(f"channel must be one of: {', '.join(self.config.channels)}")
        record = self.record()
        if record is None or not normalize_number(record.phone_on_file):
            return self._handoff("no_contact_on_file")
        destination = normalize_number(record.phone_on_file)
        signals = frozenset(self.risk.signals(record, destination)) & self.config.blocking_signals
        if signals:
            return self._handoff("contact_risk_signal", signals=sorted(signals))
        if self.sends >= self.config.max_sends_per_call:
            return self._handoff("send_limit", sends=self.sends)
        try:
            self.verifier.start(destination, channel)
        except VerifierUnavailable:
            return self._handoff("verifier_unavailable")
        self.sends += 1
        self._sent_at = self.clock()
        self._channel = channel
        return {
            "status": "code_sent",
            "channel": channel,
            "reason": GUIDANCE["code_sent"],
            "_audit": {
                "step_up": "code_sent",
                "channel": channel,
                "destination_ref": _ref(destination),
                "destination_is_caller_id": destination == normalize_number(self.caller_id),
                "sends": self.sends,
            },
        }

    def check_code(self, args: Mapping[str, Any] | None = None) -> dict:
        """verify_caller."""
        args = args or {}
        if self.verified:
            return {"status": "verified", "reason": GUIDANCE["verified"], "_audit": {"step_up": "already_verified"}}
        if self.locked:
            return self._handoff("locked")
        if self._sent_at is None:
            raise Rejected("No code has been sent on this call yet. Offer to send one first.")
        code = re.sub(r"[\s-]", "", str(args.get("code") or ""))
        if not code.isdigit() or not 4 <= len(code) <= 10:
            raise Rejected("Ask the caller for the numeric code from the message they just received.")
        if self.clock() - self._sent_at > self.config.code_ttl_seconds:
            self._sent_at = None
            raise Rejected("That code has expired. Offer to send a new one.")
        record = self.record()
        destination = normalize_number(record.phone_on_file if record else "")
        if destination and self.verifier.check(destination, code):
            self.verified = True
            self._sent_at = None
            method = f"otp_{self._channel or 'pstn'}"  # e.g. otp_sms: a restricted (PSTN) authenticator
            if self.policy is not None:
                self.policy.mark_verified(method, record.customer_id if record else None)
            return {"status": "verified", "reason": GUIDANCE["verified"], "_audit": {"step_up": "verified"}}
        self.failed_attempts += 1
        if self.failed_attempts >= self.config.max_failed_attempts:
            self._lock()
            result = self._handoff("locked", failed_attempts=self.failed_attempts)
            result["_audit"]["event"] = "step_up_locked"
            return result
        return {
            "status": "invalid_code",
            "reason": GUIDANCE["invalid_code"],
            "_audit": {"step_up": "invalid_code", "failed_attempts": self.failed_attempts},
        }

    def snapshot(self) -> dict[str, Any]:
        record = self.record()
        return {
            "verified": self.verified,
            "locked": self.locked,
            "sends": self.sends,
            "max_sends_per_call": self.config.max_sends_per_call,
            "failed_attempts": self.failed_attempts,
            "max_failed_attempts": self.config.max_failed_attempts,
            "customer_found": record is not None,
            "code_pending": self._sent_at is not None,
        }


# ---------- Construction from the environment ----------


def crm_from_env(env: Mapping[str, str] | None = None) -> CrmLookup:
    """CRM_LOOKUP_FILE (a JSON list of records) for local development; otherwise no lookup (fail closed)."""
    env = os.environ if env is None else env
    path = env.get("CRM_LOOKUP_FILE", "").strip()
    return InMemoryCrm.from_json(path) if path else NoCrm()


def verifier_from_env(env: Mapping[str, str] | None = None) -> Verifier:
    env = os.environ if env is None else env
    sid = env.get("TWILIO_VERIFY_SERVICE_SID", "").strip()
    if not sid:
        return UnavailableVerifier()
    return TwilioVerifyVerifier(
        sid, account_sid=env.get("TWILIO_ACCOUNT_SID", ""), auth_token=env.get("TWILIO_AUTH_TOKEN", "")
    )
