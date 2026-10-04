# Corey Mathie, 2026
"""
Policy gate: the first deterministic check on every tool call, before velocity.

The model proposes a tool call; this module decides whether it may run. It is
plain Python with no model in the loop, so the same inputs always give the same
decision, and every decision can be replayed from the audit log.

Checks, in order (the first one that fires wins):

  1. Allow-list, default deny. A tool must be declared in TOOL_POLICIES with a
     risk tier. Anything else (a hallucinated function name, a backend endpoint
     nobody granted the agent) is denied. POLICY_ALLOWED_TOOLS can narrow the
     list per deployment but can never add a tool that has no declared tier.
  2. Social-engineering risk. The caller's finalized transcript turns are scored
     for urgency, authority claims, requests to skip checks, attempts to redirect
     payments or contact details, and secrecy. Once the call's score reaches
     POLICY_RISK_THRESHOLD, tools at or above POLICY_HANDOFF_MIN_TIER return a
     handoff instead of running. The score is cumulative for the call.
  3. Account-takeover sequence. Once contact details on file have been changed
     on this call, a tool that moves money hands off to a person, even for a
     verified caller: change-the-contact-then-pay is a classic takeover pattern.
  4. Step-up verification. Tools at or above POLICY_STEP_UP_MIN_TIER (default
     high) need a verified session: the caller read back a one-time code sent to
     the contact on file (src/safeguards/step_up.py). Caller ID alone never
     verifies anyone. Unverified, the gate answers `step_up_required` with
     guidance to offer a code; after a step-up lockout it hands off instead.
  5. Blast radius, per call. Ceilings on what one call can do, independent of
     time windows: payment links issued, total USD across links, and completed
     state-changing actions. Velocity limits (velocity.py) are per caller and
     time-windowed and normally fire first; these caps hold even on a long call,
     and because a call lives on one process they stay correct when velocity
     state isn't shared across instances.

Decisions are allow / deny / handoff / step_up. Each carries:
  - code      a stable machine-readable reason (for dashboards and tests)
  - reason    a PII-scrubbed explanation for the audit log
  - guidance  text the model can act on; it never reveals thresholds or which
              phrases were flagged, so a caller can't tune their script

Configuration: the defaults below are the code's built-in policy. In
production the reviewed policy file config/policy.yaml is loaded at startup by
policy_config.py (schema-validated, fails closed) and produces a PolicyConfig
carrying its own tool tiers and risk signals; tests prove the shipped file
matches these defaults. POLICY_* environment variables can still adjust a
loaded policy (narrowing the allow-list, never extending it).

Pure standard library plus pii_redactor, so the browser demo can run this exact
file under Pyodide.
"""

from __future__ import annotations

import math
import os
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from .pii_redactor import redact

ALLOW, DENY, HANDOFF, STEP_UP = "allow", "deny", "handoff", "step_up"
TIERS: tuple[str, ...] = ("low", "medium", "high")
STEP_UP_OFF = "off"  # POLICY_STEP_UP_MIN_TIER value that disables step-up verification

# ---------- Tool registry: risk tiers ----------


@dataclass(frozen=True)
class ToolPolicy:
    tier: str
    state_changing: bool = True
    moves_money: bool = False
    why: str = ""
    changes_contact: bool = False  # changes the phone/email on file (account-takeover sequence rule)


TOOL_POLICIES: dict[str, ToolPolicy] = {
    "log_lead": ToolPolicy("low", why="upserts a CRM contact; no outbound message"),
    "create_ticket": ToolPolicy("medium", why="writes caller-supplied free text into a system staff trust"),
    "book_meeting": ToolPolicy("medium", why="emails a calendar invitation to a caller-supplied address"),
    "take_payment": ToolPolicy(
        "high", moves_money=True, why="creates a payment link and texts it, or starts keypad card capture"
    ),
    "update_contact": ToolPolicy(
        "high",
        changes_contact=True,
        why="changes the phone or email on file, the usual first step of an account takeover",
    ),
    "send_verification_code": ToolPolicy(
        "low",
        state_changing=False,
        why="sends a one-time code to the contact on file only; rate-limited against SMS pumping",
    ),
    "verify_caller": ToolPolicy("low", state_changing=False, why="checks a one-time code; marks the call verified"),
}

# Tool statuses that mean the action actually happened (used for per-call counters).
# sms_failed: the link exists. keypad_started: the call is in Twilio <Pay> for this amount (counted when it starts,
# because the result arrives on a new media stream; conservative for the per-call caps).
PAYMENT_ISSUED = frozenset({"link_sent", "link_created", "sms_failed", "keypad_started"})
CONTACT_CHANGED = frozenset({"updated"})
COMPLETED = frozenset({"booked", "created", "logged"}) | PAYMENT_ISSUED | CONTACT_CHANGED

# ---------- Social-engineering signals ----------


@dataclass(frozen=True)
class RiskSignal:
    name: str
    weight: int
    patterns: tuple[re.Pattern, ...]


def _p(*patterns: str) -> tuple[re.Pattern, ...]:
    return tuple(re.compile(p, re.IGNORECASE) for p in patterns)


SIGNALS: tuple[RiskSignal, ...] = (
    RiskSignal(
        "urgency",
        1,
        _p(
            r"\b(urgent(ly)?|emergency|asap|immediately|hurry|no time|right (now|away)|as fast as (you|possible))\b",
            r"\bbefore (it'?s|it is) too late\b",
        ),
    ),
    RiskSignal(
        "authority",
        2,
        _p(
            r"\b(i'?m|i am|this is) (the |a |an |your )?(owner|ceo|cfo|president|manager|supervisor|director|"
            r"administrator|admin|officer|detective|auditor|investigator)\b",
            r"\b(from|with|calling from) (the )?(irs|fbi|police|sheriff'?s office|bank|fraud (department|team)|"
            r"security (department|team)|it department|head office|corporate)\b",
            r"\bon behalf of (the |my )?(owner|ceo|bank|management|boss)\b",
            r"\b(i'?m|i am) authori[sz]ed\b",
            r"\bmy boss (said|told|wants|needs)\b",
        ),
    ),
    RiskSignal(
        "skip_checks",
        3,
        _p(
            r"\b(skip|bypass|override|disable|turn off|get around) (the |your |all |any |those )?"
            r"(verification|security|checks?|confirmation|rules?|limits?|polic(y|ies)|process|protocol|steps?)\b",
            r"\b(don'?t|do not|no need to|you don'?t need to|you do not need to) (verify|confirm|check|validate)\b",
            r"\bignore (your|the|all|any|previous|prior) (instructions|rules|polic(y|ies)|limits|guidelines|"
            r"system prompt)\b",
            r"\b(make|do) an exception\b",
            r"\bjust this once\b",
        ),
    ),
    RiskSignal(
        "redirect",
        3,
        _p(
            r"\b(send|text|email|e-mail|forward)( it| the link| the payment link| that| the code| the refund)? "
            r"(instead )?to (a |my |this |another |a different |the other )?(different|another|other|new|alternate|"
            r"second|friend'?s|wife'?s|husband'?s|partner'?s|son'?s|daughter'?s|boss'?s|assistant'?s|colleague'?s)"
            r" ?(phone |cell )?(number|phone|cell|email|e-mail|address|account)\b",
            r"\b(send|text|email|forward) (it|the link|the payment link|that|the code) to (my|his|her|their) "
            r"(friend|colleague|assistant|boss|wife|husband|partner|son|daughter)\b",
            r"\b(change|update|switch) (my |the )?(phone number|number on file|email on file|bank account|"
            r"account number|routing number|payout account|deposit account)\b",
            r"\b(gift cards?|wire (it|the money|transfer)|bitcoin|crypto(currency)?|western union)\b",
        ),
    ),
    RiskSignal(
        "secrecy",
        2,
        _p(
            r"\b(don'?t|do not) (tell|mention|inform|notify|let) (anyone|anybody|my|the|your)\b",
            r"\bkeep (this|it) (between us|quiet|secret|confidential)\b",
        ),
    ),
)

MAX_RISK_SCORE = 20


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\u2019", "'").replace("\u2018", "'")).strip().lower()


@dataclass(frozen=True)
class RiskAssessment:
    score: int
    signals: dict[str, int]  # signal name -> number of turns it fired in

    @property
    def signal_names(self) -> tuple[str, ...]:
        return tuple(sorted(self.signals))


def score_turn(text: str, signals: tuple[RiskSignal, ...] = SIGNALS) -> dict[str, int]:
    """Signals in one caller turn. Each signal counts once per turn, however often it matches."""
    t = _normalize(text)
    return {s.name: s.weight for s in signals if any(p.search(t) for p in s.patterns)}


def score_turns(
    turns: Iterable[str], signals: tuple[RiskSignal, ...] = SIGNALS, max_score: int = MAX_RISK_SCORE
) -> RiskAssessment:
    """Cumulative score across a call's caller turns, capped at max_score."""
    total = 0
    fired: dict[str, int] = {}
    for turn in turns:
        for name, weight in score_turn(turn, signals).items():
            total += weight
            fired[name] = fired.get(name, 0) + 1
    return RiskAssessment(min(total, max_score), fired)


# ---------- Configuration ----------


@dataclass(frozen=True)
class PolicyConfig:
    allowed_tools: frozenset[str] = frozenset(TOOL_POLICIES)
    max_payment_links_per_call: int = 4
    max_usd_per_call: float = 5000.0
    max_actions_per_call: int = 8
    risk_threshold: int = 4
    handoff_min_tier: str = "high"
    step_up_min_tier: str = "high"  # lowest tier that needs a verified caller; "off" disables step-up
    # The tool registry and risk signals this config enforces (from config/policy.yaml when loaded from a file).
    tool_policies: Mapping[str, ToolPolicy] = field(default_factory=lambda: dict(TOOL_POLICIES))
    signals: tuple[RiskSignal, ...] = SIGNALS
    max_risk_score: int = MAX_RISK_SCORE

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, base: PolicyConfig | None = None) -> PolicyConfig:
        """
        Read POLICY_* variables on top of `base` (the loaded policy file, or the code defaults).
        Anything missing or malformed keeps the base value.
        """
        env = os.environ if env is None else env
        d = base if base is not None else cls()

        def num(key: str, default, cast, minimum):
            try:
                value = cast(env[key])
            except (KeyError, TypeError, ValueError):
                return default
            if isinstance(value, float) and not math.isfinite(value):
                return default
            return value if value >= minimum else default

        allowed = d.allowed_tools
        raw = env.get("POLICY_ALLOWED_TOOLS")
        if raw is not None and raw.strip():
            # Env can only narrow the allow-list; a tool without a declared tier stays denied.
            allowed = frozenset(t.strip() for t in raw.split(",") if t.strip()) & d.allowed_tools
        tier = env.get("POLICY_HANDOFF_MIN_TIER", d.handoff_min_tier).strip().lower()
        step_up = env.get("POLICY_STEP_UP_MIN_TIER", d.step_up_min_tier).strip().lower()
        return replace(
            d,
            allowed_tools=allowed,
            max_payment_links_per_call=num("POLICY_MAX_PAYMENT_LINKS_PER_CALL", d.max_payment_links_per_call, int, 0),
            max_usd_per_call=num("POLICY_MAX_USD_PER_CALL", d.max_usd_per_call, float, 0),
            max_actions_per_call=num("POLICY_MAX_ACTIONS_PER_CALL", d.max_actions_per_call, int, 0),
            risk_threshold=num("POLICY_RISK_THRESHOLD", d.risk_threshold, int, 1),
            handoff_min_tier=tier if tier in TIERS else d.handoff_min_tier,
            step_up_min_tier=step_up if step_up in (*TIERS, STEP_UP_OFF) else d.step_up_min_tier,
        )

    def needs_step_up(self, tier: str) -> bool:
        if self.step_up_min_tier not in TIERS:
            return False
        return TIERS.index(tier) >= TIERS.index(self.step_up_min_tier)


# ---------- Decisions ----------

GUIDANCE = {
    "unknown_tool": (
        "That action isn't available on this line. Don't try it again; tell the caller what you can help with instead."
    ),
    "tool_disabled": (
        "That action is turned off for this line. Don't try it again; offer to have a team member follow up."
    ),
    "social_engineering_risk": (
        "A team member needs to handle this request. Don't retry it on this call. Tell the caller a person will "
        "follow up using the contact details already on file, and don't promise a time."
    ),
    "payment_link_cap": (
        "This call has reached its limit for payment links. Don't retry. Offer to have a team member follow up."
    ),
    "usd_cap": (
        "That would take this call over its total payment limit. Don't retry. Offer to have a team member follow up "
        "to arrange the rest."
    ),
    "action_cap": (
        "This call has reached its limit for actions. Don't retry. Offer to have a team member follow up on anything "
        "else the caller needs."
    ),
    "step_up_required": (
        "The caller has to be verified first. Offer to send a one-time code to the phone number already on file "
        "(send_verification_code), then pass the code they read back to verify_caller, then try again. Don't ask "
        "for a phone number and don't send the code anywhere else."
    ),
    "step_up_locked": (
        "The caller couldn't be verified on this call. Don't retry. Tell the caller a team member will follow up "
        "using the contact details already on file."
    ),
    "contact_change_then_payment": (
        "A team member needs to handle this request. Don't retry it on this call. Tell the caller a person will "
        "follow up using the contact details already on file, and don't promise a time."
    ),
    "ok": "",
}

STATUS_FOR = {DENY: "denied", HANDOFF: "require_human", STEP_UP: "step_up_required"}


@dataclass(frozen=True)
class Decision:
    action: str
    code: str
    tool: str
    reason: str
    tier: str | None = None
    risk_score: int = 0
    risk_signals: tuple[str, ...] = ()

    @property
    def allowed(self) -> bool:
        return self.action == ALLOW

    @property
    def guidance(self) -> str:
        return GUIDANCE.get(self.code, "")

    def result(self) -> dict[str, str]:
        """What the model gets back when the gate stops a call."""
        return {"status": STATUS_FOR[self.action], "reason": self.guidance}

    def audit_fields(self) -> dict[str, Any]:
        return {
            "decision": self.action,
            "code": self.code,
            "reason": self.reason,
            "tier": self.tier,
            "risk_score": self.risk_score,
            "risk_signals": list(self.risk_signals),
        }


def _safe_tool_name(name: str) -> str:
    """Tool names come from the model; scrub and bound them before they reach a log."""
    return redact(str(name))[0][:64]


def _amount(args: Mapping[str, Any]) -> float | None:
    try:
        value = float(args.get("amount_usd"))
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value > 0 else None


@dataclass
class CallPolicy:
    """
    Per-call policy state. Create one per phone call and share it across all of
    that call's tool handlers, so the caps count everything the call does.

    Caller speech reaches the scorer two ways:
      - transcript_source: a callable returning the call's finalized caller turns
        (bot.py reads them from the Pipecat LLM context at decision time), and
      - observe_caller_turn(): push one turn explicitly (evals, demo, tests).
    """

    config: PolicyConfig = field(default_factory=PolicyConfig)
    transcript_source: Callable[[], Iterable[str]] | None = None
    call_id: str | None = None
    payment_links_issued: int = 0
    usd_issued: float = 0.0
    actions_completed: int = 0
    # Step-up verification state, set by src/safeguards/step_up.py (never by caller ID).
    verified: bool = False
    verified_method: str | None = None
    customer_id: str | None = None
    step_up_locked: bool = False
    contact_changed: bool = False
    _turns: list[str] = field(default_factory=list)

    @classmethod
    def from_env(cls, **kw) -> CallPolicy:
        return cls(config=PolicyConfig.from_env(), **kw)

    # -- transcript --

    def observe_caller_turn(self, text: str) -> None:
        if text and text.strip():
            self._turns.append(text)

    def caller_turns(self) -> list[str]:
        turns = list(self._turns)
        if self.transcript_source is not None:
            turns += [t for t in self.transcript_source() if t and t.strip()]
        return turns

    def risk(self) -> RiskAssessment:
        return score_turns(self.caller_turns(), self.config.signals, self.config.max_risk_score)

    # -- step-up verification --

    def mark_verified(self, method: str, customer_id: str | None = None) -> None:
        """Record that the caller passed step-up verification on this call (called by StepUpSession)."""
        self.verified = True
        self.verified_method = method
        self.customer_id = customer_id

    def mark_step_up_locked(self) -> None:
        self.step_up_locked = True

    # -- decisions --

    def decide(self, tool: str, args: Mapping[str, Any] | None = None) -> Decision:
        args = args or {}
        cfg = self.config
        policy = cfg.tool_policies.get(tool)
        risk = self.risk()
        base = {"risk_score": risk.score, "risk_signals": risk.signal_names}
        if policy is None:
            name = _safe_tool_name(tool)
            return Decision(DENY, "unknown_tool", name, f"{name!r} has no declared policy (default deny)", **base)
        tier = policy.tier
        if tool not in cfg.allowed_tools:
            return Decision(DENY, "tool_disabled", tool, f"{tool} is not in POLICY_ALLOWED_TOOLS", tier, **base)

        if risk.score >= cfg.risk_threshold and TIERS.index(tier) >= TIERS.index(cfg.handoff_min_tier):
            signals = ", ".join(risk.signal_names)
            reason = (
                f"caller risk score {risk.score} >= threshold {cfg.risk_threshold} (signals: {signals}); "
                f"{tool} is tier {tier}"
            )
            return Decision(HANDOFF, "social_engineering_risk", tool, reason, tier, **base)

        if policy.moves_money and self.contact_changed:
            reason = f"contact details on file were changed earlier in this call; {tool} moves money"
            return Decision(HANDOFF, "contact_change_then_payment", tool, reason, tier, **base)

        if cfg.needs_step_up(tier) and not self.verified:
            if self.step_up_locked:
                reason = f"step-up verification locked on this call; {tool} is tier {tier}"
                return Decision(HANDOFF, "step_up_locked", tool, reason, tier, **base)
            reason = f"{tool} is tier {tier}; step-up verification required at tier {cfg.step_up_min_tier} and above"
            return Decision(STEP_UP, "step_up_required", tool, reason, tier, **base)

        if policy.moves_money:
            if self.payment_links_issued >= cfg.max_payment_links_per_call:
                reason = (
                    f"{self.payment_links_issued} payment links already issued (limit {cfg.max_payment_links_per_call})"
                )
                return Decision(HANDOFF, "payment_link_cap", tool, reason, tier, **base)
            amount = _amount(args)
            # The first link's size is validated by the payment handler (MAX_PAYMENT_USD);
            # the gate bounds the cumulative total once money has gone out on this call.
            if amount is not None and self.usd_issued > 0 and self.usd_issued + amount > cfg.max_usd_per_call:
                reason = (
                    f"${self.usd_issued:,.2f} already issued + ${amount:,.2f} requested exceeds the per-call "
                    f"limit of ${cfg.max_usd_per_call:,.2f}"
                )
                return Decision(HANDOFF, "usd_cap", tool, reason, tier, **base)

        if policy.state_changing and self.actions_completed >= cfg.max_actions_per_call:
            reason = f"{self.actions_completed} actions already completed (limit {cfg.max_actions_per_call})"
            return Decision(HANDOFF, "action_cap", tool, reason, tier, **base)

        return Decision(ALLOW, "ok", tool, "within policy", tier, **base)

    def record_outcome(self, tool: str, args: Mapping[str, Any] | None, status: str | None) -> None:
        """Count what actually happened. Rejected, failed, duplicate, and blocked calls don't count."""
        policy = self.config.tool_policies.get(tool)
        if policy is None or status not in COMPLETED:
            return
        if policy.state_changing:
            self.actions_completed += 1
        if policy.changes_contact and status in CONTACT_CHANGED:
            self.contact_changed = True
        if policy.moves_money and status in PAYMENT_ISSUED:
            self.payment_links_issued += 1
            self.usd_issued += _amount(args or {}) or 0.0

    def snapshot(self) -> dict[str, Any]:
        risk = self.risk()
        return {
            "risk_score": risk.score,
            "risk_threshold": self.config.risk_threshold,
            "risk_signals": dict(risk.signals),
            "payment_links_issued": self.payment_links_issued,
            "max_payment_links_per_call": self.config.max_payment_links_per_call,
            "usd_issued": round(self.usd_issued, 2),
            "max_usd_per_call": self.config.max_usd_per_call,
            "actions_completed": self.actions_completed,
            "max_actions_per_call": self.config.max_actions_per_call,
            "verified": self.verified,
            "verified_method": self.verified_method,
            "step_up_min_tier": self.config.step_up_min_tier,
            "step_up_locked": self.step_up_locked,
            "contact_changed": self.contact_changed,
        }
