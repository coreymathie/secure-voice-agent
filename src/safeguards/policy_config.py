# Corey Mathie, 2026
"""
Policy as code: load, validate, and apply config/policy.yaml.

The file declares the tool registry (tiers and what each tool can do), per-call
caps, social-engineering signals (weights and patterns), the risk threshold,
step-up settings, and velocity rules. load_policy() validates it against a
strict schema (pydantic; unknown keys are errors) plus cross-checks the schema
can't express (regexes compile, velocity rules name declared tools), and turns it
into the same objects the code already uses: a PolicyConfig, velocity rules, and
a StepUpConfig.

Fail closed: anything wrong raises PolicyConfigError with every problem listed
by field path. The voice process loads the policy at startup (active_policy()),
so an invalid file stops it from starting rather than running with a guess.

    python -m src.safeguards.policy_config config/policy.yaml    # validate, summarize, print the hash

POLICY_FILE selects the file. If it isn't set and config/policy.yaml doesn't
exist (an installation without the config directory), the code defaults are
used; tests/test_policy_config.py proves the shipped file and the code defaults
are identical.
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .policy_gate import PolicyConfig, RiskSignal, ToolPolicy
from .step_up import StepUpConfig
from .velocity import DEFAULT_RULES, VelocityRule

DEFAULT_POLICY_PATH = Path(__file__).resolve().parents[2] / "config" / "policy.yaml"
SUPPORTED_VERSIONS = (1,)

Tier = Literal["low", "medium", "high"]


class PolicyConfigError(Exception):
    """The policy file is missing, unreadable, or invalid. The process must not start with it."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ToolSpec(_Strict):
    tier: Tier
    state_changing: bool = True
    moves_money: bool = False
    changes_contact: bool = False
    enabled: bool = True
    why: str = ""


class Caps(_Strict):
    max_payment_links_per_call: int = Field(ge=0)
    max_usd_per_call: float = Field(ge=0, allow_inf_nan=False)
    max_actions_per_call: int = Field(ge=0)


class SignalSpec(_Strict):
    weight: int = Field(ge=0, le=100)
    patterns: list[str] = Field(min_length=1)

    @field_validator("patterns")
    @classmethod
    def _compiles(cls, patterns: list[str]) -> list[str]:
        for p in patterns:
            try:
                re.compile(p, re.IGNORECASE)
            except re.error as e:
                raise ValueError(f"pattern {p!r} doesn't compile: {e}") from e
        return patterns


class Risk(_Strict):
    threshold: int = Field(ge=1)
    handoff_min_tier: Tier
    max_score: int = Field(ge=1)
    signals: dict[str, SignalSpec]


class StepUp(_Strict):
    min_tier: Literal["low", "medium", "high", "off"]
    max_failed_attempts: int = Field(ge=1)
    max_sends_per_call: int = Field(ge=1)
    code_ttl_seconds: int = Field(ge=1)
    channels: list[Literal["sms", "call"]] = Field(min_length=1)
    blocking_signals: list[str] = []


class VelocitySpec(_Strict):
    tool: str
    max_count: int = Field(ge=0)
    window_seconds: int = Field(ge=1)
    action: Literal["deny", "require_human", "flag"] = "deny"


class PolicyFile(_Strict):
    version: int
    tools: dict[str, ToolSpec] = Field(min_length=1)
    caps: Caps
    risk: Risk
    step_up: StepUp
    velocity: list[VelocitySpec] = []

    @field_validator("version")
    @classmethod
    def _supported(cls, v: int) -> int:
        if v not in SUPPORTED_VERSIONS:
            raise ValueError(f"unsupported policy version {v}; this code reads {SUPPORTED_VERSIONS}")
        return v


@dataclass(frozen=True)
class LoadedPolicy:
    policy: PolicyConfig
    velocity_rules: tuple[VelocityRule, ...]
    step_up: StepUpConfig
    source: str  # file path, or "built-in defaults"
    sha256: str  # of the file bytes ("" for the built-in defaults)

    @property
    def version_ref(self) -> str:
        return self.sha256[:12] if self.sha256 else "defaults"


def _to_runtime(doc: PolicyFile, source: str, digest: str) -> LoadedPolicy:
    problems = [f"velocity: rule for undeclared tool {r.tool!r}" for r in doc.velocity if r.tool not in doc.tools]
    if problems:
        raise PolicyConfigError(f"{source}: " + "; ".join(problems))
    tools = {
        name: ToolPolicy(
            tier=t.tier,
            state_changing=t.state_changing,
            moves_money=t.moves_money,
            why=t.why,
            changes_contact=t.changes_contact,
        )
        for name, t in doc.tools.items()
    }
    signals = tuple(
        RiskSignal(name, s.weight, tuple(re.compile(p, re.IGNORECASE) for p in s.patterns))
        for name, s in doc.risk.signals.items()
    )
    policy = PolicyConfig(
        allowed_tools=frozenset(n for n, t in doc.tools.items() if t.enabled),
        max_payment_links_per_call=doc.caps.max_payment_links_per_call,
        max_usd_per_call=float(doc.caps.max_usd_per_call),
        max_actions_per_call=doc.caps.max_actions_per_call,
        risk_threshold=doc.risk.threshold,
        handoff_min_tier=doc.risk.handoff_min_tier,
        step_up_min_tier=doc.step_up.min_tier,
        tool_policies=tools,
        signals=signals,
        max_risk_score=doc.risk.max_score,
    )
    velocity = tuple(VelocityRule(r.tool, r.max_count, r.window_seconds, r.action) for r in doc.velocity)
    step_up = StepUpConfig(
        max_sends_per_call=doc.step_up.max_sends_per_call,
        max_failed_attempts=doc.step_up.max_failed_attempts,
        code_ttl_seconds=doc.step_up.code_ttl_seconds,
        channels=tuple(doc.step_up.channels),
        blocking_signals=frozenset(doc.step_up.blocking_signals),
    )
    return LoadedPolicy(policy, velocity, step_up, source, digest)


def _format(e: ValidationError) -> str:
    lines = []
    for err in e.errors():
        where = ".".join(str(p) for p in err["loc"]) or "(top level)"
        lines.append(f"  {where}: {err['msg']}")
    return "\n".join(lines)


def parse_policy(text: str, source: str = "<string>") -> LoadedPolicy:
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise PolicyConfigError(f"{source}: not valid YAML: {e}") from e
    if not isinstance(raw, dict):
        raise PolicyConfigError(f"{source}: expected a mapping at the top level")
    try:
        doc = PolicyFile.model_validate(raw)
    except ValidationError as e:
        raise PolicyConfigError(f"{source}: invalid policy\n{_format(e)}") from e
    return _to_runtime(doc, source, hashlib.sha256(text.encode()).hexdigest())


def load_policy(path: str | Path) -> LoadedPolicy:
    p = Path(path)
    try:
        text = p.read_text()
    except OSError as e:
        raise PolicyConfigError(f"{p}: can't read the policy file: {e}") from e
    return parse_policy(text, str(p))


def builtin_policy() -> LoadedPolicy:
    """The code defaults, as a LoadedPolicy (used only when no policy file exists)."""
    return LoadedPolicy(PolicyConfig(), DEFAULT_RULES, StepUpConfig(), "built-in defaults", "")


_ACTIVE: LoadedPolicy | None = None


def active_policy(env: dict | None = None, *, reload: bool = False) -> LoadedPolicy:
    """The process's policy: POLICY_FILE, else config/policy.yaml, else the code defaults. Cached."""
    global _ACTIVE
    if _ACTIVE is not None and not reload and env is None:
        return _ACTIVE
    env = os.environ if env is None else env
    explicit = env.get("POLICY_FILE", "").strip()
    if explicit:
        loaded = load_policy(explicit)  # set explicitly: a missing file is an error
    elif DEFAULT_POLICY_PATH.exists():
        loaded = load_policy(DEFAULT_POLICY_PATH)
    else:
        loaded = builtin_policy()
    if env is os.environ:
        _ACTIVE = loaded
    return loaded


def summary(loaded: LoadedPolicy) -> str:
    p = loaded.policy
    lines = [
        f"policy: {loaded.source} (sha256 {loaded.sha256 or '-'})",
        f"tools ({len(p.tool_policies)}): "
        + ", ".join(f"{n}={t.tier}{'' if n in p.allowed_tools else ' (disabled)'}" for n, t in p.tool_policies.items()),
        f"caps: {p.max_payment_links_per_call} payment links, ${p.max_usd_per_call:,.0f}, "
        f"{p.max_actions_per_call} actions per call",
        f"risk: threshold {p.risk_threshold}, handoff at {p.handoff_min_tier}+, signals "
        + ", ".join(f"{s.name}={s.weight}" for s in p.signals),
        f"step-up: {p.step_up_min_tier}+ (off disables), {loaded.step_up.max_failed_attempts} attempts, "
        f"{loaded.step_up.max_sends_per_call} sends, {loaded.step_up.code_ttl_seconds}s codes",
        f"velocity: {len(loaded.velocity_rules)} rules",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    path = args[0] if args else str(DEFAULT_POLICY_PATH)
    try:
        loaded = load_policy(path)
    except PolicyConfigError as e:
        print(f"INVALID {e}", file=sys.stderr)
        return 1
    print(summary(loaded))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
